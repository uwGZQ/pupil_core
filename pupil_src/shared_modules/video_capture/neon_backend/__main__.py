# __main__.py, which will be executed when the module is run with -m. ex. python -m package
import contextlib
import ctypes
import multiprocessing
import os
import pathlib
import sys
from typing import Tuple

from .background import BackgroundCameraSharingManager
from .definitions import NEON_SHARED_EYE_FRAME_TOPIC


def main():
    # multiprocessing.Value 是一个可以在多个进程之间共享的变量。初始化时，需要指定变量的类型。
    timebase = multiprocessing.Value(ctypes.c_double)
    # 将 timebase.value 设置为 0.0
    timebase.value = 0.0

    ipc_pub_url, ipc_push_url, ipc_sub_url = ipc()

    manager = BackgroundCameraSharingManager(
        timebase=timebase,
        ipc_pub_url=ipc_pub_url,
        ipc_push_url=ipc_push_url,
        ipc_sub_url=ipc_sub_url,
        topic_prefix=NEON_SHARED_EYE_FRAME_TOPIC,
    )
    with contextlib.suppress(KeyboardInterrupt):
        # 等待子进程结束，然后再继续执行。
        manager._background_process.join()
    manager.stop()


def ipc() -> Tuple[str, str, str]:
    from threading import Thread

    import zmq # ZeroMQ 是一个高性能的异步消息库，用于构建分布式或并发应用。

    zmq_ctx = zmq.Context()

    # Let the OS choose the IP and PORT
    ipc_pub_url = "tcp://*:*"
    ipc_sub_url = "tcp://*:*"
    ipc_push_url = "tcp://*:*"

    # Binding IPC Backbone Sockets to URLs.
    # They are used in the threads started below.
    # Using them in the main thread is not allowed.
    #  IP 地址从 "0.0.0.0" 更改为 "127.0.0.1"，以便只能从本地访问。
    xsub_socket = zmq_ctx.socket(zmq.XSUB)
    xsub_socket.bind(ipc_pub_url)
    ipc_pub_url = xsub_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    xpub_socket = zmq_ctx.socket(zmq.XPUB)
    xpub_socket.bind(ipc_sub_url)
    ipc_sub_url = xpub_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    pull_socket = zmq_ctx.socket(zmq.PULL)
    pull_socket.bind(ipc_push_url)
    ipc_push_url = pull_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    # Starting communication threads:
    # A ZMQ Proxy Device serves as our IPC Backbone
    """
    启动了四个线程：这些线程使用之前创建的套接字进行通信。
    Target ::= Function to be executed by thread.
    Args ::= Arguments passed to the target function.
    Daemon ::= If true, the thread will be terminated when the main thread exits.
    """
    ipc_backbone_thread = Thread(
        target=zmq.proxy, args=(xsub_socket, xpub_socket), daemon=True
    )
    ipc_backbone_thread.start()

    pull_pub = Thread(
        target=pull_pub_thread, args=(ipc_pub_url, pull_socket), daemon=True
    )
    pull_pub.start()

    log_thread = Thread(target=log_loop_thread, args=(ipc_sub_url, True), daemon=True)
    log_thread.start()

    delay_thread = Thread(
        target=delay_proxy_thread, args=(ipc_push_url, ipc_sub_url), daemon=True
    )
    delay_thread.start()

    # 并不直接删除对象本身，而是删除了变量名和对象之间的关联。
    # 释放变量名，仍然保持对象
    del xsub_socket, xpub_socket, pull_socket

    return ipc_pub_url, ipc_push_url, ipc_sub_url


# Reliable msg dispatch to the IPC via push bridge.
def pull_pub_thread(ipc_pub_url, pull):
    import zmq

    # 创建了一个 ZeroMQ 上下文
    ctx = zmq.Context.instance()
    # ZeroMQ PUB 套接字。PUB 套接字用于发布消息，任何连接到它的 SUB 套接字都可以接收到这些消息
    pub = ctx.socket(zmq.PUB)
    # 将 PUB 套接字连接到指定的 URL
    pub.connect(ipc_pub_url)

    while True:
        """收到一个消息，然后群发给所有连接到这个 PUB 套接字的 SUB 套接字"""
        # 从 PULL 套接字接收一个多部分的消息。在 ZeroMQ 中，你可以发送和接收由多个部分组成的消息，这对于发送复杂的数据结构非常有用。
        m = pull.recv_multipart()
        # 通过 PUB 套接字发送接收到的多部分消息。所有连接到这个 PUB 套接字的 SUB 套接字都会接收到这个消息。
        pub.send_multipart(m)


# The delay proxy handles delayed notififications.
def delay_proxy_thread(ipc_pub_url, ipc_sub_url):
    import zmq
    import zmq_tools

    ctx = zmq.Context.instance()
    # 只会接收主题为 "delayed_notify"的消息
    sub = zmq_tools.Msg_Receiver(ctx, ipc_sub_url, ("delayed_notify",))
    pub = zmq_tools.Msg_Dispatcher(ctx, ipc_pub_url)
    # Poller 对象用于监视套接字的状态，当套接字有数据可读时，poller.poll() 方法会返回 True。
    poller = zmq.Poller()
    poller.register(sub.socket, zmq.POLLIN)
    # 一个空字典，用于存储待发送的延迟通知。
    waiting_notifications = {}

    TOPIC_CUTOFF = len("delayed_")

    while True:
        # 如果 SUB 套接字有数据可读，那么接收新的延迟通知，并将其存储在字典中。通知的发送时间是当前时间加上延迟时间。
        if poller.poll(timeout=250):
            # Recv new delayed notification and store it.
            topic, n = sub.recv()
            n["__notify_time__"] = time() + n["delay"]
            waiting_notifications[n["subject"]] = n
        # When a notifications time has come, pop from dict and send it as notification
        # 遍历字典中的所有通知，如果通知的发送时间已经到达，那么从字典中删除该通知，并通过 PUB 套接字发送出去。
        for s, n in list(waiting_notifications.items()):
            if n["__notify_time__"] < time():
                n["topic"] = n["topic"][TOPIC_CUTOFF:]
                del n["__notify_time__"]
                del n["delay"]
                del waiting_notifications[s]
                pub.notify(n)


# Recv log records from other processes.
def log_loop_thread(ipc_sub_url, log_level_debug):
    import logging

    import zmq
    import zmq_tools
    from rich.logging import RichHandler

    # Get the root logger
    logger = logging.getLogger()
    # set log level
    logger.setLevel(logging.NOTSET)
    # Stream to file
    fh = logging.FileHandler("neon_backend.log", mode="w", encoding="utf-8")
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(processName)s - [%(levelname)s] %(name)s: %(message)s"
        )
    )
    logger.addHandler(fh)
    # Stream to console.

    ch = RichHandler(
        level=logging.DEBUG if log_level_debug else logging.INFO,
        rich_tracebacks=False,
    )
    ch.setFormatter(logging.Formatter("%(processName)s - %(name)s: %(message)s"))

    logger.addHandler(ch)
    # IPC setup to receive log messages. Use zmq_tools.ZMQ_handler to send messages to here.
    sub = zmq_tools.Msg_Receiver(zmq.Context(), ipc_sub_url, topics=("logging",))
    while True:
        topic, msg = sub.recv()
        record = logging.makeLogRecord(msg)
        logger.handle(record)


if __name__ == "__main__":
    shared_modules = pathlib.Path(__file__).parent.parent.parent
    print(f"{shared_modules=}")
    sys.path.append(str(shared_modules))
    main()

"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import os
import platform
import sys

# 冻结状态通常指的是将Python程序打包成独立可执行文件，以便在没有安装Python解释器的情况下运行。
running_from_bundle = getattr(sys, "frozen", False)

# 以python源码运行时，将shared_modules添加到sys.path中
if not running_from_bundle:
    # pupil/
    pupil_base_dir = os.path.abspath(__file__).rsplit("pupil_src", 1)[0]
    # pupil/pupil_src, pupil/shared_modules
    sys.path.append(os.path.join(pupil_base_dir, "pupil_src", "shared_modules"))

# locate in shared_modules
from launchable_args import PupilArgParser

# NOTE: hyphens (-) in the CLI args are converted to underscores (_) upon parsing, so
# "--hide-ui" becomes "hide_ui" in python
default_args = {
    "debug": False,
    "profile": False,
    "version": False,
    "hide_ui": False,
    "port": 50020,
    "skip_driver_installation": False,
}
# parsed_args: 命名空间对象，包含已知参数的解析结果
# unknown_args: list，包含未知参数
parsed_args, unknown_args = PupilArgParser().parse(running_from_bundle, **default_args)

# app version
from version_utils import get_version

# retrieve version from version file: Version class or LegacyVersion class
app_version = get_version()
if parsed_args.version:
    running_from = "bundle" if running_from_bundle else "source"
    version_message = (
        # 让app这个参数字符串的首字母大写
        f"Pupil {parsed_args.app.capitalize()} version {app_version} ({running_from})"
    )

    print(version_message)
    sys.exit()


def set_bundled_glfw_environ_var():
    import pathlib

    meipass = pathlib.Path(sys._MEIPASS)
    glfw_folder = meipass / "glfw"
    lib_path = next(glfw_folder.glob("*glfw*"), None)
    if lib_path is None:
        session_type = (
            "wayland" if os.environ.get("XDG_SESSION_TYPE") == "wayland" else "x11"
        )
        lib_path = glfw_folder / session_type / "libglfw.so"
    os.environ["PYGLFW_LIBRARY"] = str(lib_path)


if running_from_bundle:
    # Specifiy user dir.
    folder_name = f"pupil_{parsed_args.app}_settings"
    user_dir = os.path.expanduser(os.path.join("~", folder_name))

    # set libglfw env variable to prevent endless version check loop within pyglfw
    set_bundled_glfw_environ_var()
else:
    # Specifiy user dir.
    user_dir = os.path.join(pupil_base_dir, f"{parsed_args.app}_settings")

    # Add pupil_external binaries to PATH
    if platform.system() == "Windows":
        import os
        import pathlib

        path_external = (
            pathlib.Path(__file__) / ".." / ".." / "pupil_external"
        ).resolve()
        print(f"Adding {path_external} to PATH")
        os.environ["PATH"] += f"{os.pathsep}{path_external}"

# create folder for user settings, tmp data
if not os.path.isdir(user_dir):
    os.mkdir(user_dir)

# create folder for user plugins
plugin_dir = os.path.join(user_dir, "plugins")
if not os.path.isdir(plugin_dir):
    os.mkdir(plugin_dir)

from ctypes import c_bool, c_double

# threading and processing
from multiprocessing import (
    Process,
    Value,  # Value is a ctypes object that can be shared between processes
    active_children,
    freeze_support,
    set_start_method,
)
from threading import Thread

# time
from time import time

# networking
import zmq
import zmq_tools

# os utilities
from os_utils import Prevent_Idle_Sleep

# functions to run in seperate processes
if parsed_args.profile:
    from launchables.eye import eye_profiled as eye
    from launchables.player import player_profiled as player
    from launchables.service import service_profiled as service
    from launchables.world import world_profiled as world
else:
    from launchables.world import world
    from launchables.service import service
    from launchables.eye import eye
    from launchables.player import player

from launchables.marker_detectors import circle_detector
from launchables.player import player_drop


def clear_settings(user_dir):
    import glob
    import os
    import time

    time.sleep(1.0)
    for f in glob.glob(os.path.join(user_dir, "user_settings_*")):
        print(f"Clearing {f}...")
        os.remove(f)
    time.sleep(5)


def launcher():
    """Starts eye processes. Hosts the IPC Backbone and Logging functions.

    Reacts to notifications:
       ``launcher_process.should_stop``: Stops the launcher process
       ``eye_process.should_start``: Starts the eye process
    """

    # Reliable msg dispatch to the IPC via push bridge.
    # Defines a nested function pull_pub, which is responsible for receiving messages from a ZMQ PULL socket 
    # and publishing them to a ZMQ PUB socket. 
    # This function essentially acts as a message relay. 消息中继
    def pull_pub(ipc_pub_url, pull):
        # Setup IPC to publish messages to other processes.
        ctx = zmq.Context.instance()
        pub = ctx.socket(zmq.PUB)
        pub.connect(ipc_pub_url)

        while True:
            m = pull.recv_multipart()
            pub.send_multipart(m)

    # The delay proxy handles delayed notififications.
    # delay_proxy, handles delayed notifications. It listens for notifications and dispatches them after a specified delay.
    def delay_proxy(ipc_pub_url, ipc_sub_url):
        ctx = zmq.Context.instance()
        sub = zmq_tools.Msg_Receiver(ctx, ipc_sub_url, ("delayed_notify",))
        pub = zmq_tools.Msg_Dispatcher(ctx, ipc_pub_url)
        poller = zmq.Poller()
        poller.register(sub.socket, zmq.POLLIN)
        waiting_notifications = {}

        TOPIC_CUTOFF = len("delayed_")

        while True:
            if poller.poll(timeout=250):
                # Recv new delayed notification and store it.
                topic, n = sub.recv()
                n["__notify_time__"] = time() + n["delay"]
                waiting_notifications[n["subject"]] = n
            # When a notifications time has come, pop from dict and send it as notification
            for s, n in list(waiting_notifications.items()):
                if n["__notify_time__"] < time():
                    n["topic"] = n["topic"][TOPIC_CUTOFF:]
                    del n["__notify_time__"]
                    del n["delay"]
                    del waiting_notifications[s]
                    pub.notify(n)

    # Recv log records from other processes.
    # This function sets up logging for the application. It configures logging handlers, formats, and levels, and receives log messages from other processes over a ZMQ socket.
    def log_loop(ipc_sub_url, log_level_debug):
        import logging

        from rich.logging import RichHandler

        # Get the root logger
        logger = logging.getLogger()
        # set log level
        logger.setLevel(logging.NOTSET)
        # Stream to file
        fh = logging.FileHandler(
            os.path.join(user_dir, f"{parsed_args.app}.log"),
            mode="w",
            encoding="utf-8",
        )
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
        ch.setFormatter(
            logging.Formatter("%(processName)s - %(name)s: %(message)s", datefmt="[%X]")
        )

        logger.addHandler(ch)
        # IPC setup to receive log messages. Use zmq_tools.ZMQ_handler to send messages to here.
        sub = zmq_tools.Msg_Receiver(zmq.Context(), ipc_sub_url, topics=("logging",))
        while True:
            topic, msg = sub.recv()
            record = logging.makeLogRecord(msg)
            logger.handle(record)

    ## IPC
    timebase = Value(c_double, 0)
    # eye_procs_alive is a list of boolean values that indicate whether an eye process is running or not.
    eye_procs_alive = Value(c_bool, 0), Value(c_bool, 0)

    zmq_ctx = zmq.Context()

    # Let the OS choose the IP and PORT
    ipc_pub_url = "tcp://*:*"
    ipc_sub_url = "tcp://*:*"
    ipc_push_url = "tcp://*:*"

    # Binding IPC Backbone Sockets to URLs.
    # They are used in the threads started below.
    # Using them in the main thread is not allowed.

    # 发布者
    xsub_socket = zmq_ctx.socket(zmq.XSUB)
    xsub_socket.bind(ipc_pub_url)
    ipc_pub_url = xsub_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    # 订阅者
    xpub_socket = zmq_ctx.socket(zmq.XPUB)
    xpub_socket.bind(ipc_sub_url)
    ipc_sub_url = xpub_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    # 管道是单向的，从PUSH端单向的向PULL端单向的推送数据流。
    pull_socket = zmq_ctx.socket(zmq.PULL)
    pull_socket.bind(ipc_push_url)
    ipc_push_url = pull_socket.last_endpoint.decode("utf8").replace(
        "0.0.0.0", "127.0.0.1"
    )

    # Starting communication threads:
    # A ZMQ Proxy Device serves as our IPC Backbone
        # This proxy acts as a central communication point, facilitating message exchange between XSUB and XPUB sockets.
    ipc_backbone_thread = Thread(
        target=zmq.proxy, args=(xsub_socket, xpub_socket), daemon=True
    )
    ipc_backbone_thread.start()

        # Runs a thread that pulls messages from a PULL socket and publishes them to a PUB socket. This is a part of the IPC mechanism to relay messages from one part of the system to another.
    pull_pub = Thread(target=pull_pub, args=(ipc_pub_url, pull_socket), daemon=True)
    pull_pub.start()

        # Initiates a logging thread which sets up logging handlers (file and console) and listens for logging messages on a ZMQ SUB socket. It processes these messages and logs them appropriately.
    log_thread = Thread(
        target=log_loop, args=(ipc_sub_url, parsed_args.debug), daemon=True
    )
    log_thread.start()

        # tarts a thread for handling delayed notifications. It listens for specific messages and dispatches them after a delay, as specified in the messages.
    delay_thread = Thread(
        target=delay_proxy, args=(ipc_push_url, ipc_sub_url), daemon=True
    )
    delay_thread.start()

    del xsub_socket, xpub_socket, pull_socket

    #  Each topic typically corresponds to a different function or component in the system
    topics = (
        "notify.eye_process.",
        "notify.player_process.",
        "notify.world_process.",
        "notify.service_process",
        "notify.clear_settings_process.",
        "notify.player_drop_process.",
        "notify.launcher_process.",
        "notify.meta.should_doc",
        "notify.circle_detector_process.should_start",
        "notify.ipc_startup",
    )
    # It is initialized with the ZMQ context (zmq_ctx), a subscription URL (ipc_sub_url), and the list of topics it should subscribe to (topics).
    # This receiver will listen for messages sent over ZMQ that match any of the specified topics.
    cmd_sub = zmq_tools.Msg_Receiver(zmq_ctx, ipc_sub_url, topics=topics)
    # This dispatcher is used to send messages to other parts of the application. It will publish messages that can be received by any subscriber listening to the corresponding topic.
    cmd_push = zmq_tools.Msg_Dispatcher(zmq_ctx, ipc_push_url)

    while True:
        # Wait until subscriptions were successfull
        cmd_push.notify({"subject": "ipc_startup"})
        if cmd_sub.socket.poll(timeout=50):
            cmd_sub.recv()
            break

    import logging

    if unknown_args:
        logging.warning(f"Unknown command-line arguments: {unknown_args}")

    if parsed_args.app == "service":
        cmd_push.notify({"subject": "service_process.should_start"})
    elif parsed_args.app == "capture":
        cmd_push.notify({"subject": "world_process.should_start"})
    elif parsed_args.app == "player":
        rec_dir = os.path.expanduser(parsed_args.recording)
        cmd_push.notify(
            {"subject": "player_drop_process.should_start", "rec_dir": rec_dir}
        )


    # The main loop of the function. 
        # It continuously checks for incoming messages and 
        # handles them appropriately. 
        # The Prevent_Idle_Sleep context manager is likely used to prevent the system from going into idle mode while the application is running.
        # The loop handles notifications, starts/stops processes, and manages the application state.
    with Prevent_Idle_Sleep():
        try:
            while True:
                # listen for relevant messages.
                if cmd_sub.socket.poll(timeout=1000):
                    topic, n = cmd_sub.recv()
                    process_notification(
                        topic,
                        n,
                        timebase,
                        eye_procs_alive,
                        ipc_pub_url,
                        ipc_sub_url,
                        ipc_push_url,
                        cmd_push,
                        user_dir,
                        app_version,
                        parsed_args,
                    )
                else:
                    if not active_children():
                        break
        except KeyboardInterrupt:
            logging.warning(
                f"Launcher shutting down with active children: {active_children()}"
            )
            # TODO: join with timeout and kill after
        else:
            for p in active_children():
                p.join()


# responsible for processing various notifications. It starts different processes based on the notifications, like eye tracking, player, world, etc., using the Process class from the multiprocessing module.
def process_notification(
    topic,
    notification,
    timebase,
    eye_procs_alive,
    ipc_pub_url,
    ipc_sub_url,
    ipc_push_url,
    cmd_push,
    user_dir,
    app_version,
    parsed_args,
):
    # If the notification topic indicates that an eye process should start, it launches an eye tracking process with specific parameters.
    if "notify.eye_process.should_start" in topic:
        eye_id = notification["eye_id"]
        Process(
            target=eye,
            name=f"eye{eye_id}",
            args=(
                timebase,
                eye_procs_alive[eye_id],
                ipc_pub_url,
                ipc_sub_url,
                ipc_push_url,
                user_dir,
                app_version,
                eye_id,
                notification.get("overwrite_cap_settings"),
                parsed_args.hide_ui,
                parsed_args.debug,
                notification.get("pub_socket_hwm"),
                parsed_args.app,  # parent_application
                parsed_args.skip_driver_installation,
            ),
        ).start()
    elif "notify.player_process.should_start" in topic:
        Process(
            target=player,
            name="player",
            args=(
                notification["rec_dir"],
                ipc_pub_url,
                ipc_sub_url,
                ipc_push_url,
                user_dir,
                app_version,
                parsed_args.debug,
            ),
        ).start()
    elif "notify.world_process.should_start" in topic:
        Process(
            target=world,
            name="world",
            args=(
                timebase,
                eye_procs_alive,
                ipc_pub_url,
                ipc_sub_url,
                ipc_push_url,
                user_dir,
                app_version,
                parsed_args.port,
                parsed_args.hide_ui,
                parsed_args.debug,
                parsed_args.skip_driver_installation,
            ),
        ).start()
    elif "notify.clear_settings_process.should_start" in topic:
        Process(target=clear_settings, name="clear_settings", args=(user_dir,)).start()
    elif "notify.service_process.should_start" in topic:
        Process(
            target=service,
            name="service",
            args=(
                timebase,
                eye_procs_alive,
                ipc_pub_url,
                ipc_sub_url,
                ipc_push_url,
                user_dir,
                app_version,
                parsed_args.port,
                parsed_args.hide_ui,
                parsed_args.debug,
            ),
        ).start()
    elif "notify.player_drop_process.should_start" in topic:
        Process(
            target=player_drop,
            name="player",
            args=(
                notification["rec_dir"],
                ipc_pub_url,
                ipc_sub_url,
                ipc_push_url,
                user_dir,
                app_version,
                parsed_args.debug,
            ),
        ).start()
    elif "notify.circle_detector_process.should_start" in topic:
        Process(
            target=circle_detector,
            name="circle_detector",
            args=(ipc_push_url, notification["pair_url"], notification["source_path"]),
        ).start()
    elif "notify.meta.should_doc" in topic:
        cmd_push.notify(
            {
                "subject": "meta.doc",
                "actor": "launcher",
                "doc": launcher.__doc__,
            }
        )
    elif "notify.launcher_process.should_stop" in topic:
        if parsed_args.app == "capture":
            cmd_push.notify({"subject": "world_process.should_stop"})
        elif parsed_args.app == "service":
            cmd_push.notify({"subject": "service_process.should_stop"})
        elif parsed_args.app == "player":
            cmd_push.notify({"subject": "player_process.should_stop"})


if __name__ == "__main__":
    freeze_support()
    set_start_method("spawn")
    launcher()

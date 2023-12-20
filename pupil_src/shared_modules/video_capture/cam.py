import cv2
import numpy as np
import sys
import pathlib
# 将当前目录上一级的上一级目录加入到系统路径中
# name '__file__' is not defined
# SCENE_CAM_SPEC = CameraSpec(
#     name="Neon Scene Camera v1",
#     vendor_id=0x0BDA,
#     product_id=0x3036,
#     width=1280,
#     height=720,
#     fps=30,
#     bandwidth_factor=1.2,
# )
sys.path.append("/Users/gaoziqi/Desktop/EyeTrack/pupil_core/pupil_src/shared_modules")
from neon_backend.camera import NeonCameraInterface, SCENE_CAM_SPEC
# print(SCENE_CAM_SPEC)
from neon_backend.definitions import CameraSpec

# SCENE_CAM_SPEC = CameraSpec(
#     name="Endoplus",
#     vendor_id=3141,  # 0x0C45
#     product_id=25771,  # 0x6483
#     width=1280,
#     height=720,
#     fps=30,
#     bandwidth_factor=1.2,
# )
# SCENE_CAM_SPEC = CameraSpec(
#     name="Pupil Cam1 ID2",
#     vendor_id=1443,  # 0x0C45
#     product_id=37426,  # 0x6483
#     width=1280,
#     height=720,
#     fps=30,
#     bandwidth_factor=1.2,
#     # uid="20:22",
# )

SCENE_CAM_SPEC = CameraSpec(
    name="USB Camera",
    vendor_id=3034,  # 0x0C45
    product_id=1383,  # 0x6483
    width=492,
    height=492,
    fps=120,
    bandwidth_factor=1.2,
    # uid="20:22",
)

import uvc

print("device_list",uvc.device_list(),"\n\n\n")
# print(uvc.Capture('20:23'),"\n\n\n")

def capture_and_show_video(camera_spec):
    with NeonCameraInterface(camera_spec) as camera:
        print("Starting video capture...")
        try:
            while True:
                frame = camera.get_shared_frame(0.03)  # 0.03秒的超时时间
                if frame is not None and frame.data_fully_received:
                    # 转换为OpenCV图像格式
                    image = np.array(frame.gray, dtype=np.uint8)
                    cv2.imshow('Camera Stream', image)

                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
        except Exception as e:
            print("Error here:", e)
        except KeyboardInterrupt:
            print("Stopping video capture...")
        else:
            print("Video capture stopped.")
            print("Error:", camera)


    cv2.destroyAllWindows()

# 使用示例
capture_and_show_video(SCENE_CAM_SPEC)

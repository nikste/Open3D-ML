#!/usr/bin/env python

import json
import time
import logging as log
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import open3d as o3d
import open3d.visualization.gui as gui
import open3d.visualization.rendering as rendering
from open3d.ml.utils import Config
from open3d.ml.vis import BoundingBox3D, LabelLUT
from open3d.ml import datasets
BEVBox3D = datasets.utils.bev_box.BEVBox3D

import gtimer as gt
import ipdb


class pipeline_gui(object):
    """ GUI for the frame pipeline """

    def __init__(self, labels=None, vfov=60):
        """ Initialize GUI

        Args:
            labels: List of class labels for the detector
            vfov: Camera vertical field of view in degrees
        """
        self.vfov = vfov
        self.material = o3d.visualization.rendering.Material()
        self.material.shader = "defaultLit"
        self.lut = LabelLUT()

        gui.Application.instance.initialize()
        self.window = gui.Application.instance.create_window(
            "Open3D || 3D Object Detection", 1024, 768)
        self.window.set_on_close(self._on_window_close)
        em = self.window.theme.font_size

        # layout = gui.Vert()
        # self.window.add_child(layout)  # window can have only one child

        # 3D scene
        self.scene = gui.SceneWidget()
        self.window.add_child(self.scene)
        self.scene.enable_scene_caching(True)  # makes UI _much_ more responsive
        self.scene.scene = rendering.Open3DScene(self.window.renderer)
        self.scene.scene.set_background([1, 1, 1, 1])  # White brackground
        self.scene.scene.show_axes(True)
        self._reset_view()

        # Options panel
        self.options_window = gui.Application.instance.create_window(
            "Open3D || 3D Object Detection || Options", 128, 256)
        self.options_window.set_on_close(self._on_window_close)
        panel = gui.Vert(em / 2, gui.Margins(em, em, em, em))
        self.options_window.add_child(panel)
        panel.add_stretch()  # before first child

        self.flag_capture = False
        self.cv_capture = threading.Condition()
        toggle_capture = gui.Checkbox("Capture")
        toggle_capture.checked = self.flag_capture
        toggle_capture.set_on_checked(self._on_toggle_capture)
        panel.add_child(toggle_capture)

        self.flag_detector = False
        if labels:
            for val in sorted(labels):
                self.lut.add_label(val, val)
            toggle_detect = gui.Checkbox("Run Detector")
            toggle_detect.checked = self.flag_detector
            toggle_detect.set_on_checked(self._on_toggle_detector)
            panel.add_child(toggle_detect)

        reset_view = gui.Button("Reset view")
        reset_view.set_on_clicked(self._reset_view)
        panel.add_child(reset_view)
        panel.add_stretch()  # after last child

        self.flag_exit = False
        self.flag_gui_empty = True

    def update(self, frame_elements):
        """Update visualization with point cloud and bounding boxes
        Must run in main thread since this makes GUI calls

        Args:
            frame_elements: dict {label: geometry element}
                Dictionary of labels to geometry elements to be updated in the
                GUI
        """
        if not self.flag_gui_empty:
            for name in frame_elements.keys():
                self.scene.scene.remove_geometry(name)
        else:
            self.flag_gui_empty = False

        # Add point cloud and bounding boxes
        for name, element in frame_elements.items():
            self.scene.scene.add_geometry(name, element, self.material)
        self.scene.force_redraw()
        gt.stamp(name="GUI", unique=False)

    def _on_window_close(self):
        """ Callback when the user closes the application window """
        self.flag_exit = True
        with self.cv_capture:
            self.cv_capture.notify_all()
        return True  # OK to close window

    def _on_toggle_detector(self, is_enabled):
        """ Callback to toggle the detector """
        self.flag_detector = is_enabled

    def _on_toggle_capture(self, is_enabled):
        """ Callback to toggle capture """
        self.flag_capture = is_enabled
        if is_enabled:
            with self.cv_capture:
                self.cv_capture.notify()

    def _reset_view(self):
        """ Callback to reset point cloud view to the camera """
        # Point cloud bounds, depend on the sensor range
        pcd_bounds = o3d.geometry.AxisAlignedBoundingBox([-3, -3, 0], [3, 3, 6])
        self.scene.setup_camera(self.vfov, pcd_bounds, [0, 0, 0])
        # Look at [0, 0, 1] from an eye placed at [0, 0, 0] with Y axis
        # pointing at [0, -1, 0]
        self.scene.scene.camera.look_at([0, 0, 1], [0, 0, 0], [0, -1, 0])


class frame_pipeline(object):
    """Capture RGBD frames, convert to point cloud, run detector and show
    bounding boxes overlayed on the Point Cloud"""

    def __init__(self,
                 detector_config_file,
                 device=None,
                 camera_config_file=None):

        # Detector
        if device:
            self.device = device
        else:
            self.device = 'cuda' if o3d.core.cuda.is_available() else 'cpu'
        if detector_config_file:
            self.detector_config = Config.load_from_file(detector_config_file)
            if self.detector_config['model']['ckpt_path'].endswith('.pth'):
                import open3d.ml.torch as ml3d
                self.run_detector = self._run_detector_torch
                log.info("Using PyTorch for inference")

            else:
                import open3d.ml.tf as ml3d
                self.run_detector = self._run_detector_tf
                log.info("Using Tensorflow for inference")

            self.net = ml3d.models.PointPillars(**self.detector_config.model,
                                                device=self.device)
            if self.run_detector is self._run_detector_torch:
                self.net.eval()

            log.info(
                f"Loaded model from {self.detector_config['model']['ckpt_path']}"
            )
        else:
            self.run_detector = None
            log.info("No model provided. Detector disabled.")
        self.det_inputs = None

        # Depth camera
        self.rscam = o3d.t.io.RealSenseSensor()
        if camera_config_file:
            with open(camera_config_file) as ccf:
                self.rscam.init_sensor(
                    o3d.t.io.RealSenseSensorConfig(json.load(ccf)))

        self.rscam.start_capture()
        self.rgbd_metadata = self.rscam.get_metadata()
        self.max_points = self.rgbd_metadata.width * self.rgbd_metadata.height
        log.info(self.rgbd_metadata)

        # RGBD -> PCD
        self.extrinsics = o3d.core.Tensor.eye(4, dtype=o3d.core.Dtype.Float32)
        self.intrinsic_matrix = o3d.core.Tensor(
            self.rgbd_metadata.intrinsics.intrinsic_matrix,
            dtype=o3d.core.Dtype.Float32)
        self.calib = {
            'world_cam':
                self.extrinsics.numpy(),
            'cam_img':
                np.hstack((self.intrinsic_matrix.numpy(),
                           np.zeros((3, 1), dtype=np.float32))).T
        }
        self.depth_max = 3.0  # m
        self.pcd_stride = 1  # downsample point cloud

        # GUI
        labels = self.detector_config['model'][
            'classes'] if self.run_detector else None
        vfov = np.rad2deg(2 * np.arctan(self.intrinsic_matrix[1, 2].item() /
                                        self.intrinsic_matrix[1, 1].item()))
        self.gui = pipeline_gui(labels=labels, vfov=vfov)

    def _run_detector_torch(self, pcd_frame):
        """ Run PyTorch 3D detector """
        import torch

        with torch.no_grad():
            if self.det_inputs is None:
                self.det_inputs = torch.ones((1, self.max_points, 4),
                                             dtype=torch.float32,
                                             device=self.device)
            # gray_image_vector = rgbd_frame.color.as_tensor().numpy().mean(
            #     axis=2).reshape((-1, 1))
            # Add fake reflectance data
            pcd_points = pcd_frame.point['points']
            self.det_inputs[0, :pcd_points.shape[0], :3] = torch.as_tensor(
                pcd_points.numpy(), dtype=torch.float32, device=self.device)

            gt.stamp("DepthToPCDPost", unique=False)
            #results = self.net(self.det_inputs[:,:pcd_points.shape[0],:])
            #boxes = self.net.inference_end(results, {
            #    'point': pcd_points,
            #    'calib': self.calib
            #})

        test_box = BEVBox3D([1, 1, 1], [0.3, 0.3, 0.3], 0, 'Pedestrian', 1,
                            self.calib['world_cam'], self.calib['cam_img'])
        return [test_box]

    def _run_detector_tf(self, pcd_frame):
        """ Run Tensorflow 3D detector """
        import tensorflow as tf

        inputs = tf.convert_to_tensor(data_frame['point'], dtype=np.float32)
        inputs = tf.reshape(inputs, (1, -1, inputs.shape[-1]))

        results = self.net(inputs, training=False)
        boxes = self.net.inference_end(results, data_frame)
        return boxes

    def launch(self):
        """ Launch frame pipeline thread and start GUI """
        threading.Thread(name='FramePipeline', target=self._run).start()
        gui.Application.instance.run()

    def _run(self):
        """ Run pipeline """
        with ThreadPoolExecutor(max_workers=1,
                                thread_name_prefix='Capture') as executor:
            gt.stamp('Startup')
            t1 = time.perf_counter()
            frame_id = 0
            rgbd_frame = self.rscam.capture_frame(wait=True,
                                                  align_depth_to_color=True)
            while frame_id < 1000 and not self.gui.flag_exit:
                future_rgbd_frame = executor.submit(self.rscam.capture_frame,
                                                    wait=True,
                                                    align_depth_to_color=True)
                gt.stamp("SubmitCapture", unique=False)

                pcd_frame = o3d.t.geometry.PointCloud.create_from_depth_image(
                    rgbd_frame.depth, self.intrinsic_matrix, self.extrinsics,
                    self.rgbd_metadata.depth_scale, self.depth_max,
                    self.pcd_stride)
                frame_elements = {self.rgbd_metadata.serial_number: pcd_frame}
                gt.stamp("DepthToPCD", unique=False)
                if pcd_frame.is_empty():
                    log.warning(f"No valid depth data in frame {frame_id})")
                    continue
                if self.gui.flag_detector:
                    bboxes = self.run_detector(pcd_frame)
                    lines = BoundingBox3D.create_lines(bboxes, self.gui.lut)
                    frame_elements['Boxes'] = lines
                    # log.info(bboxes)
                    gt.stamp("Detector", unique=False)

                gui.Application.instance.post_to_main_thread(
                    self.gui.window, lambda: self.gui.update(frame_elements))

                rgbd_frame = future_rgbd_frame.result()
                gt.stamp("GetCapture", unique=False)
                if frame_id % 10 == 0:
                    t0, t1 = t1, time.perf_counter()
                    print(
                        f"\nframe_id = {frame_id}, {(t1-t0)*100:0.2f} ms/frame",
                        end='')

                with self.gui.cv_capture:  # Wait for capture to be enabled
                    self.gui.cv_capture.wait_for(
                        predicate=lambda: self.gui.flag_capture or self.gui.
                        flag_exit)
                gt.stamp("UserWait", unique=False)
                frame_id += 1

        self.rscam.stop_capture()
        log.info(gt.report())


if __name__ == "__main__":

    log.basicConfig(level=log.DEBUG)
    parser = argparse.ArgumentParser(
        description=R"""Online 3D object detection pipeline\n\n
    - Connects to a depth camera (currently RealSense)\n
    - Captures color and depth frames\n
    - Convert frames to point cloud\n
    - Run object detector on point cloud\n
    - Visualize results""")
    parser.add_argument('--camera-config',
                        help='Depth camera configuration JSON file')
    parser.add_argument('--detector-config',
                        required=False,
                        help='Detector configuration YAML file')
    parser.add_argument('--device',
                        help='Device to run model inference. '
                        'Default is CUDA if available, else CPU.')

    args = parser.parse_args()

    frame_pipeline(args.detector_config, args.device,
                   args.camera_config).launch()

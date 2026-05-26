import math
from statistics import mean, stdev
import logging
import numpy as np
from enum import Enum, auto
from collections import namedtuple

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
class KtamvPm:
    def __init__(self, config):
        self.speed = config.getfloat("move_speed", 6000.0, above=10.0)
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.toolhead = self.printer.lookup_object("toolhead")

        xconfig = config.getsection('stepper_x')
        yconfig = config.getsection('stepper_y')
        self._x_range = (xconfig.getfloat('position_min'), xconfig.getfloat('position_max'))
        self._y_range = (yconfig.getfloat('position_min'), yconfig.getfloat('position_max'))

    def ensureHomed(self, home=True) -> bool:
        curtime = self.printer.get_reactor().monotonic()
        toolhead = self.printer.lookup_object("toolhead")
        kin_status = toolhead.get_kinematics().get_status(curtime)
        if (
            "x" not in kin_status["homed_axes"]
            or "y" not in kin_status["homed_axes"]
            or "z" not in kin_status["homed_axes"]
        ):
            if home:
                self.gcode.run_script_from_command("G28")
                self.toolhead.wait_moves()
                return True
            else:
                return False

    def moveRelative(self, X=0, Y=0, Z=0, protected=False):
        self.ensureHomed()
        _current_position = self.get_gcode_position()
        _new_position = [_current_position[0] + X, _current_position[1] + Y]
        logging.debug(f"Current absolute position: {str(_current_position)}, move to: {str(_new_position)}")
        try:
            if not (protected):
                self.moveAbsoluteToArray(_new_position)
                self.toolhead.wait_moves()
            else:
                self.moveAbsolute(
                    _new_position[0],
                    _current_position[1],
                    _current_position[2]
                )
                self.toolhead.wait_moves()
                self.moveAbsolute(
                    _new_position[0], _new_position[1], _current_position[2]
                )
                self.toolhead.wait_moves()
                self.moveAbsolute(
                    _new_position[0], _new_position[1], _new_position[2]
                )
                self.toolhead.wait_moves()
        except Exception as e:
            logging.error(f"Error in moveRelative: {str(e)}")
            raise self.gcode.error(f"moveRelative failed: {str(e)}")

    def moveAbsoluteToArray(self, pos_array):
        clamped_pos = []
        for i in range(len(pos_array)):
            if i == 0:
                clamped_pos.append(max(self._x_range[0], min(pos_array[i], self._x_range[1])))
            elif i == 1:
                clamped_pos.append(max(self._y_range[0], min(pos_array[i], self._y_range[1])))
            else:
                clamped_pos.append(pos_array[i])
        
        gcode = "G90\nG1 "
        for i in range(len(clamped_pos)):
            if i == 0:
                gcode += "X%s " % (clamped_pos[i])
            elif i == 1:
                gcode += "Y%s " % (clamped_pos[i])
            elif i == 2:
                gcode += "Z%s " % (clamped_pos[i])
        gcode += "F%s " % (self.speed)

        self.gcode.run_script_from_command(gcode)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()

    def moveAbsolute(self, X=None, Y=None, Z=None):
        current_pos = self.get_gcode_position()
        pos_array = [
            X if X is not None else current_pos[0],
            Y if Y is not None else current_pos[1],
            Z if Z is not None else current_pos[2]
        ]
        self.moveAbsoluteToArray(pos_array)

    def get_gcode_position(self):
        gcode_move = self.printer.lookup_object("gcode_move")
        gcode_position = gcode_move.get_status()["gcode_position"]

        return [gcode_position.x, gcode_position.y, gcode_position.z]

    def get_raw_position(self):
        gcode_move = self.printer.lookup_object("gcode_move")
        raw_position = gcode_move.get_status()["position"]

        return [raw_position.x, raw_position.y, raw_position.z]

class CalibrationStep(Enum):
    INITIALIZE = auto()
    START_CAMERA_CALIBRATION = auto()
    WAIT_CAMERA_CALIBRATION = auto()
    START_T0_NOZZLE_CALIBRATION = auto()
    WAIT_T0_NOZZLE_CALIBRATION = auto()
    SET_ORIGIN_AND_SWITCH_TOOL = auto()
    MOVE_TO_CENTER_T1 = auto()
    START_T1_NOZZLE_CALIBRATION = auto()
    WAIT_T1_NOZZLE_CALIBRATION = auto()
    CALCULATE_AND_SAVE_OFFSET = auto()
    COMPLETE = auto()

class CalibrationState(Enum):
    IDLE = "idle"
    CAMERA_CALIBRATION = "camera_calibration"
    NOZZLE_CALIBRATION = "nozzle_calibration"
    SIMPLE_POSITION = "simple_position"
    AUTO_CAMERA_CENTER = "auto_camera_center"

CalibrationPoint = namedtuple('CalibrationPoint', ['space', 'camera', 'mpp'])
class CalibrationData:
    def __init__(self):
        self.points = []
        self.transform_matrix = None
        self.average_mpp = None

    def add_point(self, space_coord, camera_coord, mpp):
        self.points.append(CalibrationPoint(space_coord, camera_coord, mpp))

    def clear(self):
        self.points.clear()
        self.transform_matrix = None
        self.average_mpp = None

    @property
    def space_coordinates(self):
        return [point.space for point in self.points]

    @property
    def camera_coordinates(self):
        return [point.camera for point in self.points]

    @property
    def mm_per_pixels(self):
        return [point.mpp for point in self.points]

    @property
    def transform_input(self):
        return [(point.space, Ktamv_Utl.normalize_coords(point.camera))
                for point in self.points]

class CameraCalibrationSession:
    def __init__(self):
        self.points = [
            [-1.0, 1.0], [0, -2.0], [2.0, 2.0], [-2.0, -1.0], [2.0, -1.0],
            [-1.0, 2.0], [1.0, -1.0], [-1.0, -1.0], [-0.5, 0.5], [0.8, 0.8],
        ]
        self.current_index = 0
        self.start_xy = None
        self.start_uv = None
        self.initial_position_received = False

class Ktamv:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.camera_center_points = config.getfloatlist('camera_center_points')
        self.base_x_offset = config.getfloat('base_x_offset', 0.0)
        self.gain = config.getfloat('gain', 0.55)

        self.current_state = CalibrationState.IDLE
        self.calibration_data = CalibrationData()
        self.camera_calibration = None
        self.center_position = None

        self.operation_retries = 0
        self.calibration_retries = 0
        self.max_retries = config.getint('max_retries', 20)
        self.max_calibration_retries = config.getint('max_calibration_retries', 3)
        self.search_step = config.getfloat('search_step', 5.0, above=0.5)
        self.search_radius_x = config.getfloat('search_radius_x', 15.0, above=1.0)
        self.search_radius_y = config.getfloat('search_radius_y', 15.0, above=1.0)
        self.adjusted_gain = self.gain

        self.pm = None
        self.reactor = None
        self.utl = Ktamv_Utl()

        self.calibration_status = {
            'current_step': CalibrationStep.INITIALIZE.name,
            'step_description': 'Initializing calibration',
            'status': 'idle',
            'progress': 0
        }

        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint("ktamv/result", self._handle_webhook_result)
        self.printer.register_event_handler("klippy:ready", self._handle_klippy_ready)

    def _handle_klippy_ready(self):
        self.reactor = self.printer.get_reactor()
        self.pm = KtamvPm(self.config)
        self._register_gcode_commands()

    def _register_gcode_commands(self):
        self.gcode.register_command(
            "KTAMV_CLEAN_NOZZLE",
            self.cmd_KTAMV_CLEAN_NOZZLE,
            desc="Clean the nozzle using the configured nozzle cleaner"
        )
        self.gcode.register_command(
            "KTAMV_SET_CENTER_OFFSET",
            self.cmd_KTAMV_SET_CENTER_OFFSET,
            desc="Set the center offset for the camera calibration"
        )
        self.gcode.register_command(
            "KTAMV_MOVE_DATUM_CENTER",
            self.cmd_KTAMV_MOVE_DATUM_CENTER,
            desc="Move the datum center to the specified position"
        )
        self.gcode.register_command(
            "KTAMV_CALIB_NOZZLE",
            self.cmd_KTAMV_CALIB_NOZZLE,
            desc=self.cmd_KTAMV_CALIB_NOZZLE_help,
        )
        self.gcode.register_command(
            "KTAMV_CALIB_CAMERA",
            self.cmd_KTAMV_CALIB_CAMERA,
            desc=self.cmd_KTAMV_CALIB_CAMERA_help,
        )
        self.gcode.register_command(
            "KTAMV_FIND_NOZZLE_CENTER",
            self.cmd_KTAMV_FIND_NOZZLE_CENTER,
            desc=self.cmd_KTAMV_FIND_NOZZLE_CENTER_help,
        )
        self.gcode.register_command(
            "KTAMV_SIMPLE_NOZZLE_POSITION",
            self.cmd_KTAMV_SIMPLE_NOZZLE_POSITION,
            desc=self.cmd_KTAMV_SIMPLE_NOZZLE_POSITION_help,
        )
        self.gcode.register_command("KTAMV_SET_ORIGIN", self.cmd_KTAMV_SET_ORIGIN)
        self.gcode.register_command("KTAMV_GET_OFFSET", self.cmd_KTAMV_GET_OFFSET)
        self.gcode.register_command("KTAMV_SAVE_OFFSET", self.cmd_KTAMV_SAVE_OFFSET)
        self.gcode.register_command("KTAMV_CLEAR_STATUS", self.cmd_KTAMV_CLEAR_STATUS)


    def _handle_webhook_result(self, web_result):
        try:
            result = web_result.get_dict('objects')
            function_name = result.get('function')
            logging.info(f"Received webhook result for result: {result}")
            if function_name == 'get_nozzle_position':
                self._handle_nozzle_position_result(result)
            elif function_name == 'calculate_camera_to_space_matrix':
                self._handle_calibration_matrix_result(result)
            else:
                logging.warning(f"Unknown webhook function: {function_name}")

        except Exception as e:
            logging.error(f"Error handling webhook result: {str(e)}")

    def _handle_nozzle_position_result(self, result):
        if result.get("status") == "error":
            error_msg = result.get('message', 'Unknown error')
            logging.error(f"Nozzle position error: {error_msg}")
            if self.current_state == CalibrationState.AUTO_CAMERA_CENTER:
                self._process_auto_camera_center({"position": None})
            elif self.current_state == CalibrationState.CAMERA_CALIBRATION:
                self._process_calibration_position({"position": None})
            else:
                self._retry_camera_calibration(error_msg)
            return
        state_handlers = {
            CalibrationState.CAMERA_CALIBRATION: self._process_calibration_position,
            CalibrationState.NOZZLE_CALIBRATION: self._process_nozzle_calibration_position,
            CalibrationState.SIMPLE_POSITION: self._process_simple_position,
            CalibrationState.AUTO_CAMERA_CENTER: self._process_auto_camera_center,
        }
        handler = state_handlers.get(self.current_state)
        if handler:
            handler(result)
        else:
            logging.warning(f"No handler for nozzle position in state: {self.current_state}")

    def _handle_calibration_matrix_result(self, result):
        if result.get("status") == "success":
            self.calibration_data.transform_matrix = np.array(result['matrix'])
            self.gcode.respond_info("Camera calibration successful!")
            self._cleanup_operation()
        else:
            error_msg = result.get('message', 'Calibration matrix calculation failed')
            self._handle_operation_failure(error_msg)

    def _process_calibration_position(self, result):
        position_data = result.get("position")
        if not self.camera_calibration:
            logging.error("Camera calibration session not initialized")
            self._handle_operation_failure("Camera calibration session not initialized")
            return

        phase = getattr(self, '_calib_phase', 'searching')

        if phase == 'searching':
            self._calib_search(position_data)
        elif phase == 'verifying':
            self._calib_verify(position_data)
        elif phase == 'scaling':
            self._calib_scaling(position_data)
        elif phase == 'centering':
            self._calib_centering(position_data)
        elif phase == 'calibrating':
            self._calib_process_point(position_data)

    def _start_verify(self, position_data):
        self.gcmd.respond_info(
            f"Possible nozzle at search point {self._search_index + 1}"
            f" (UV: {position_data}), verifying...")
        self._auto_first_uv = position_data
        self._auto_first_xy = self.pm.get_gcode_position()[:2]
        self._schedule_detect()

    def _check_verification(self, position_data):
        """Returns True if verified (caller should proceed to scaling).
        Returns False if verification failed (caller should continue search)."""
        if position_data is None:
            self.gcmd.respond_info("False detection, continuing search")
            return False
        dx = abs(position_data[0] - self._auto_first_uv[0])
        dy = abs(position_data[1] - self._auto_first_uv[1])
        if dx > 50 or dy > 50:
            self.gcmd.respond_info(
                f"Detection unstable ({dx:.0f},{dy:.0f}px drift), continuing search")
            return False
        self.gcmd.respond_info(f"Nozzle verified at UV {position_data}")
        self._auto_first_uv = position_data
        self._auto_first_xy = self.pm.get_gcode_position()[:2]
        return True

    def _do_scale_move(self, target_phase):
        self._calc_scale_direction()
        setattr(self, target_phase, 'scaling')
        self.gcmd.respond_info(
            f"Centering: scale move X{self._auto_scale_dx:.1f} Y{self._auto_scale_dy:.1f}")
        self._with_slow_speed(
            lambda: self.pm.moveRelative(
                X=self._auto_scale_dx, Y=self._auto_scale_dy))
        self._schedule_detect()

    def _calib_search(self, position_data):
        if position_data is None:
            self._search_next_calibration_point()
            return
        self._calib_phase = 'verifying'
        self._start_verify(position_data)

    def _calib_verify(self, position_data):
        if self._check_verification(position_data):
            self._do_scale_move('_calib_phase')
        else:
            self._calib_phase = 'searching'
            self._search_next_calibration_point()

    def _calib_scaling(self, position_data):
        if position_data is None:
            self._handle_operation_failure("Nozzle lost after scaling move")
            return
        mpp = self._calc_signed_mpp(position_data)
        if mpp is None:
            self._handle_operation_failure("Pixel displacement too small for scale")
            return
        self._calib_mpp_x, self._calib_mpp_y = mpp
        self._calib_center_retries = 0
        self._calib_phase = 'centering'
        self.gcmd.respond_info(
            f"Scale: X{self._calib_mpp_x:.4f} Y{self._calib_mpp_y:.4f} mm/pixel")
        self._calib_centering(position_data)

    def _calib_centering(self, nozzle_uv):
        def on_centered(uv):
            self.camera_calibration.start_xy = self.pm.get_gcode_position()[:2]
            self.camera_calibration.start_uv = uv
            self.camera_calibration.initial_position_received = True
            self._calib_phase = 'calibrating'
            self._save_camera_offset_vars()
            self.gcmd.respond_info("Nozzle centered, starting calibration moves")
            self._move_to_calibration_point(0)
        self._do_centering_step(
            nozzle_uv,
            '_calib_mpp_x', '_calib_mpp_y',
            '_prev_dx_pixel', '_prev_dy_pixel',
            10, '_calib_center_retries',
            on_centered)

    def _calib_process_point(self, position_data):
        current_index = self.camera_calibration.current_index
        if current_index >= len(self.camera_calibration.points):
            return

        if position_data is not None:
            current_xy = self.pm.get_gcode_position()[:2]
            dx_space = current_xy[0] - self.camera_calibration.start_xy[0]
            dy_space = current_xy[1] - self.camera_calibration.start_xy[1]
            dist_space = math.sqrt(dx_space**2 + dy_space**2)

            dx_cam = position_data[0] - self.camera_calibration.start_uv[0]
            dy_cam = position_data[1] - self.camera_calibration.start_uv[1]
            dist_cam = math.sqrt(dx_cam**2 + dy_cam**2)

            if not self._validate_calibration_move(dist_space, dist_cam, current_index):
                self.camera_calibration.current_index += 1
                if self.camera_calibration.current_index < len(self.camera_calibration.points):
                    self._move_to_calibration_point(self.camera_calibration.current_index)
                else:
                    self._finish_camera_calibration()
                return

            if dist_cam > 1e-3:
                mpp = dist_space / dist_cam
                self.calibration_data.add_point(
                    (dx_space, dy_space),
                    (dx_cam, dy_cam),
                    mpp
                )
                self.gcmd.respond_info(
                    f"MM per pixel for step {current_index + 1} of {len(self.camera_calibration.points)} is {mpp:.6f}"
                )

        self.camera_calibration.current_index += 1
        if self.camera_calibration.current_index < len(self.camera_calibration.points):
            self._move_to_calibration_point(self.camera_calibration.current_index)
        else:
            logging.info("All calibration points processed, finishing calibration")
            self._finish_camera_calibration()

    def _validate_calibration_move(self, dist_space, dist_cam, current_index):
        if dist_space < 0.01 or dist_cam < 5:
            self.gcode.respond_info(f"Skipping invalid calibration point {current_index + 1}")
            return False
        return True

    def _process_nozzle_calibration_position(self, result):
        position_data = result.get("position")
        if position_data is None:
            self._handle_nozzle_not_found()
            return
        current_xy = self.pm.get_gcode_position()[:2]
        self._calibrate_nozzle_offset(position_data, current_xy)

    def _process_simple_position(self, result):
        position_data = result.get("position")
        runtime = result.get("runtime", 0.0)
        gcmd = getattr(self, '_simple_position_gcmd', None)
        if not gcmd:
            return
        if position_data is not None:
            gcmd.respond_info(f"Found nozzle at position: {position_data} after {runtime:.2f} seconds")
        else:
            gcmd.respond_info(f"Did not find nozzle after {runtime:.2f} seconds!")

    def _generate_search_pattern(self, step, max_rx, max_ry):
        points = [(0.0, 0.0)]
        n = 1
        while n * step <= max(max_rx, max_ry):
            rx = min(n * step, max_rx)
            ry = min(n * step, max_ry)
            if rx <= 0 and ry <= 0:
                break
            rx = round(rx, 2)
            ry = round(ry, 2)
            # Top side: left to right
            x = -rx
            while x <= rx + 0.001:
                points.append((round(x, 2), round(-ry, 2)))
                x += step
            # Right side: top+step to bottom
            y = -ry + step
            while y <= ry + 0.001:
                points.append((round(rx, 2), round(y, 2)))
                y += step
            # Bottom side: right-step to left
            x = rx - step
            while x >= -rx - 0.001:
                points.append((round(x, 2), round(ry, 2)))
                x -= step
            # Left side: bottom-step to top+step
            y = ry - step
            while y >= -ry + step - 0.001:
                points.append((round(-rx, 2), round(y, 2)))
                y -= step
            n += 1
        return points

    def _clamp_search_ry(self, center_xy):
        curtime = self.printer.get_reactor().monotonic()
        toolhead = self.printer.lookup_object("toolhead")
        kin_status = toolhead.get_kinematics().get_status(curtime)
        y_min = float(kin_status['axis_minimum'].y)
        y_max = float(kin_status['axis_maximum'].y)
        return min(self.search_radius_y,
                   max(min(center_xy[1] - y_min, y_max - center_xy[1]), 0.0))

    def _init_search(self):
        center_xy = self.pm.get_gcode_position()[:2]
        max_ry = self._clamp_search_ry(center_xy)
        self._search_points = self._generate_search_pattern(
            self.search_step, self.search_radius_x, max_ry)
        self._search_index = 0
        self._search_center = center_xy
        return center_xy, max_ry

    def _search_next_point(self, phase_name):
        self._search_index += 1
        if self._search_index >= len(self._search_points):
            self._handle_operation_failure(
                "Nozzle not found within search area.")
            return
        dx, dy = self._search_points[self._search_index]
        self.gcmd.respond_info(
            f"Searching {self._search_index + 1}/{len(self._search_points)}"
            f" offset X{dx:.1f} Y{dy:.1f}")
        self._move_to_search_point(phase_name)

    def _move_to_search_point(self, phase_name):
        dx, dy = self._search_points[self._search_index]
        target_x = self._search_center[0] + dx
        target_y = self._search_center[1] + dy
        self._with_slow_speed(lambda: self.pm.moveAbsolute(
            X=target_x, Y=target_y))
        self._schedule_detect()

    def _schedule_detect(self):
        self.reactor.register_callback(
            lambda e: self._call_remote_method("get_nozzle_position"),
            self.reactor.monotonic() + 0.3)

    def _with_slow_speed(self, fn):
        original_speed = self.pm.speed
        try:
            self.pm.speed = 500
            fn()
        finally:
            self.pm.speed = original_speed

    def _calc_scale_direction(self):
        scale_step = min(self.search_step, 1.0)
        if self._search_index > 0:
            prev = self._search_points[self._search_index - 1]
            curr = self._search_points[self._search_index]
            last_dx = curr[0] - prev[0]
            last_dy = curr[1] - prev[1]
        else:
            last_dx = self.search_step
            last_dy = 0.0
        if abs(last_dx) >= abs(last_dy):
            self._auto_scale_dx = scale_step if last_dx >= 0 else -scale_step
            self._auto_scale_dy = 0.0
        else:
            self._auto_scale_dx = 0.0
            self._auto_scale_dy = scale_step if last_dy >= 0 else -scale_step

    def _calc_signed_mpp(self, position_data):
        second_xy = self.pm.get_gcode_position()[:2]
        dx_space = second_xy[0] - self._auto_first_xy[0]
        dy_space = second_xy[1] - self._auto_first_xy[1]
        dx_pixel = position_data[0] - self._auto_first_uv[0]
        dy_pixel = position_data[1] - self._auto_first_uv[1]
        if abs(self._auto_scale_dx) > 0 and abs(dx_pixel) >= 1:
            signed_mpp = dx_space / dx_pixel
        elif abs(self._auto_scale_dy) > 0 and abs(dy_pixel) >= 1:
            signed_mpp = dy_space / dy_pixel
        else:
            return None
        if abs(signed_mpp) < 0.001:
            return None
        mpp_abs = abs(signed_mpp)
        if abs(self._auto_scale_dx) > 0:
            return (signed_mpp, mpp_abs)
        else:
            return (mpp_abs, signed_mpp)

    def _do_centering_step(self, nozzle_uv, mpp_x_attr, mpp_y_attr,
                           prev_dx_attr, prev_dy_attr, max_iters,
                           iter_attr, on_centered, threshold=5):
        if nozzle_uv is None:
            self._handle_operation_failure("Nozzle lost during centering")
            return False
        dx_pixel = FRAME_WIDTH / 2.0 - nozzle_uv[0]
        dy_pixel = FRAME_HEIGHT / 2.0 - nozzle_uv[1]
        mpp_x = getattr(self, mpp_x_attr)
        mpp_y = getattr(self, mpp_y_attr)
        prev_dx = getattr(self, prev_dx_attr, None)
        if prev_dx is not None:
            if abs(dx_pixel) > abs(prev_dx) + 3:
                mpp_x = -mpp_x
                setattr(self, mpp_x_attr, mpp_x)
                self.gcmd.respond_info("Flipped X direction sign")
            prev_dy = getattr(self, prev_dy_attr)
            if abs(dy_pixel) > abs(prev_dy) + 3:
                mpp_y = -mpp_y
                setattr(self, mpp_y_attr, mpp_y)
                self.gcmd.respond_info("Flipped Y direction sign")
        setattr(self, prev_dx_attr, dx_pixel)
        setattr(self, prev_dy_attr, dy_pixel)
        if abs(dx_pixel) < threshold and abs(dy_pixel) < threshold:
            on_centered(nozzle_uv)
            return True
        gain = self.gain
        if iter_attr:
            gain = max(self.gain - getattr(self, iter_attr) * 0.02, 0.3)
        dx_mm = round(dx_pixel * mpp_x * gain, 3)
        dy_mm = round(dy_pixel * mpp_y * gain, 3)
        self.gcmd.respond_info(
            f"Centering: UV {nozzle_uv} -> move X{dx_mm:.3f} Y{dy_mm:.3f}")
        self._with_slow_speed(lambda: self.pm.moveRelative(X=dx_mm, Y=dy_mm))
        if iter_attr:
            iters = getattr(self, iter_attr) + 1
            setattr(self, iter_attr, iters)
            if iters >= max_iters:
                on_centered(nozzle_uv)
                return True
        self._schedule_detect()
        return False

    def _process_auto_camera_center(self, result):
        position_data = result.get("position")
        phase = getattr(self, '_auto_phase', 'searching')

        if phase == 'searching':
            self._auto_center_search(position_data)
        elif phase == 'verifying':
            self._auto_center_verify(position_data)
        elif phase == 'scaling':
            self._auto_center_scaling(position_data)
        elif phase == 'centering':
            self._auto_center_iterate(position_data)

    def _auto_center_search(self, position_data):
        if position_data is not None:
            self._auto_phase = 'verifying'
            self._start_verify(position_data)
            return
        self._search_next_point('searching')

    def _auto_center_verify(self, position_data):
        if self._check_verification(position_data):
            self._do_scale_move('_auto_phase')
        else:
            self._auto_phase = 'searching'
            self._search_next_point('searching')

    def _auto_center_scaling(self, position_data):
        if position_data is None:
            self._handle_operation_failure(
                "Nozzle lost after scaling move, cannot calculate center offset")
            return
        mpp = self._calc_signed_mpp(position_data)
        if mpp is None:
            self._handle_operation_failure(
                "Pixel displacement too small, cannot calculate scale")
            return
        self._auto_mpp_x, self._auto_mpp_y = mpp
        self._auto_phase = 'centering'
        self.operation_retries = 0
        self.gcmd.respond_info(
            f"Scale: X{self._auto_mpp_x:.4f} Y{self._auto_mpp_y:.4f} mm/pixel")
        self._auto_center_iterate(position_data)

    def _auto_center_iterate(self, nozzle_uv):
        def on_centered(uv):
            self._save_camera_center_offset()
        self._do_centering_step(
            nozzle_uv,
            '_auto_mpp_x', '_auto_mpp_y',
            '_auto_prev_dx_pixel', '_auto_prev_dy_pixel',
            self.max_retries, 'operation_retries',
            on_centered, threshold=3)

    def _save_camera_offset_vars(self):
        current_pos = self.pm.get_raw_position()
        x_offset = round(
            float(current_pos[0]) - self.camera_center_points[0], 3)
        y_offset = round(
            float(current_pos[1]) - self.camera_center_points[1], 3)
        z_offset = round(
            float(current_pos[2]) - self.camera_center_points[2], 3)
        for var_name, value in [
            ("camera_x_offset_val", x_offset),
            ("camera_y_offset_val", y_offset),
            ("camera_z_offset_val", z_offset)
        ]:
            script = f'SAVE_VARIABLE VARIABLE={var_name} VALUE="{value}"'
            self.gcode.run_script_from_command(script)
        self.gcmd.respond_info(
            f"Camera offset saved: X:{x_offset:.3f} Y:{y_offset:.3f}"
            f" Z:{z_offset:.3f}")

    def _save_camera_center_offset(self):
        self._save_camera_offset_vars()
        self.gcmd.respond_info("Auto camera center calibration complete!")
        self.current_state = CalibrationState.IDLE
        self._cleanup_operation()

    cmd_KTAMV_CALIB_NOZZLE_help = (
        "Calibrates the movement of the active nozzle"
        + " around the point it started at"
    )
    def cmd_KTAMV_CALIB_NOZZLE(self, gcmd):
        gcmd.respond_info("Starting nozzle calibration")
        self.calibration_retries = 0
        try:
            self.calibration_state = {
                'step': CalibrationStep.INITIALIZE,
                'gcmd': gcmd,
                'error': None,
                'camera_calibrated': False,
            }
            self.reactor.register_callback(self._polling_step)
        except Exception as e:
            self._handle_operation_failure(f"Nozzle calibration failed: {str(e)}")

    cmd_KTAMV_CALIB_CAMERA_help = (
        "Automatically searches for the nozzle via rectangular spiral,"
        + " centers it, and records the camera center offset")
    def cmd_KTAMV_CALIB_CAMERA(self, gcmd):
        gcmd.respond_info("Starting automatic camera center calibration")
        self.pm.ensureHomed()
        self._switch_tool(0)
        self._move_to_camera_center()

        center_xy, max_ry = self._init_search()

        self.current_state = CalibrationState.AUTO_CAMERA_CENTER
        self._auto_phase = 'searching'
        self.operation_retries = 0
        self.gcmd = gcmd
        gcmd.respond_info(
            f"Search: {len(self._search_points)} points,"
            f" step={self.search_step:.1f}mm,"
            f" radius X{self.search_radius_x:.1f}/Y{max_ry:.1f}mm")
        self._call_remote_method("get_nozzle_position")

    cmd_KTAMV_FIND_NOZZLE_CENTER_help = ("Finds the center of the nozzle and moves"
        + " it to the center of the camera, offset can be set from here")
    def cmd_KTAMV_FIND_NOZZLE_CENTER(self, gcmd):
        self._start_nozzle_calibration(gcmd)

    cmd_KTAMV_SIMPLE_NOZZLE_POSITION_help = (
        "Detects if a nozzle is found in the current image")
    def cmd_KTAMV_SIMPLE_NOZZLE_POSITION(self, gcmd):
        self._start_simple_position_detection(gcmd)

    def cmd_KTAMV_SET_ORIGIN(self, gcmd):
        self.center_position = self.pm.get_raw_position()
        self.center_position = (
            round(float(self.center_position[0]), 3),
            round(float(self.center_position[1]), 3)
        )
        gcmd.respond_info(f"Center position set to X:{self.center_position[0]:.3f} Y:{self.center_position[1]:.3f}")

    def cmd_KTAMV_GET_OFFSET(self, gcmd):
        if self.center_position is None:
            raise gcmd.error("No center position set, use KTAMV_SET_ORIGIN to set it first!")
        current_pos = self.pm.get_raw_position()
        offset = (
            self.base_x_offset + round(float(current_pos[0]) - self.center_position[0], 3),
            round(float(current_pos[1]) - self.center_position[1], 3)
        )
        gcmd.respond_info(f"Offset from center is X:{offset[0]:.3f} Y:{offset[1]:.3f}")
        self._last_offset = offset

    def cmd_KTAMV_SAVE_OFFSET(self, gcmd):
        if not hasattr(self, '_last_offset'):
            gcmd.error("No offset calculated yet!")
            return
        try:
            x_new = round(float(self._last_offset[0]), 3)
            y_new = round(float(self._last_offset[1]), 3)

            self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=nozzle_x_offset_val VALUE={x_new}")
            self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=nozzle_y_offset_val VALUE={y_new}")        
            gcmd.respond_info(f"Offset saved: X:{x_new:.3f} Y:{y_new:.3f}")
        except Exception as e:
            gcmd.error(f"Failed to save offset: {str(e)}")
        
    def cmd_KTAMV_CLEAR_STATUS(self, gcmd):
        self.calibration_status = {
            'current_step': '',
            'step_description': '',
            'status': '',
            'progress': 0
        }

    def _polling_step(self, eventtime):
        interval = 0.5
        if not hasattr(self, 'calibration_state') or self.calibration_state.get('error'):
            self.calibration_status = {
                'current_step': 'ERROR',
                'step_description': self.calibration_state.get('error'),
                'status': 'error',
                'progress': 0
            }
            return

        state = self.calibration_state
        gcmd = state['gcmd']

        try:
            current_step = state['step']

            step_details = {
                CalibrationStep.INITIALIZE: {'desc': 'Homing and cleaning the nozzle', 'progress': 0},
                CalibrationStep.START_CAMERA_CALIBRATION: {'desc': 'Starting camera calibration', 'progress': 5},
                CalibrationStep.WAIT_CAMERA_CALIBRATION: {'desc': 'Waiting for camera calibration', 'progress': 35},
                CalibrationStep.START_T0_NOZZLE_CALIBRATION: {'desc': 'Starting T0 nozzle calibration', 'progress': 5},
                CalibrationStep.WAIT_T0_NOZZLE_CALIBRATION: {'desc': 'Waiting for T0 nozzle calibration', 'progress': 20},
                CalibrationStep.SET_ORIGIN_AND_SWITCH_TOOL: {'desc': 'Setting origin and switching tool', 'progress': 5},
                CalibrationStep.MOVE_TO_CENTER_T1: {'desc': 'Moving to T1 center position', 'progress': 5},
                CalibrationStep.START_T1_NOZZLE_CALIBRATION: {'desc': 'Starting T1 nozzle calibration', 'progress': 5},
                CalibrationStep.WAIT_T1_NOZZLE_CALIBRATION: {'desc': 'Waiting for T1 nozzle calibration', 'progress': 20},
                CalibrationStep.CALCULATE_AND_SAVE_OFFSET: {'desc': 'Calculating and saving offset', 'progress': 0},
                CalibrationStep.COMPLETE: {'desc': 'Calibration complete', 'progress': 0}
            }

            step_descriptions = {}
            step_progress = {}
            for step in CalibrationStep:
                details = step_details.get(step, {'desc': f'Unknown step: {step.name}', 'progress': 0})
                step_descriptions[step] = details['desc']
                step_progress[step] = details['progress']

            step_order = [step for step in CalibrationStep 
                         if step not in [CalibrationStep.INITIALIZE, 
                                        CalibrationStep.CALCULATE_AND_SAVE_OFFSET, 
                                        CalibrationStep.COMPLETE]]

            if current_step == CalibrationStep.INITIALIZE:
                progress = 0.0
            elif current_step in [CalibrationStep.CALCULATE_AND_SAVE_OFFSET, CalibrationStep.COMPLETE]:
                progress = 100.0
            else:
                progress = 0.0
                for step in step_order:
                    progress += step_progress[step]
                    if step == current_step:
                        break
                progress = min(progress, 100.0)

            self.calibration_status = {
                'current_step': current_step.name,
                'step_description': step_descriptions.get(current_step, 'Unknown step'),
                'status': 'running',
                'progress': progress
            }

            if current_step == CalibrationStep.INITIALIZE:
                self.calibration_status['status'] = 'homing'
                self.pm.ensureHomed(True)
                self.clean_nozzle()
                state['step'] = CalibrationStep.START_CAMERA_CALIBRATION

            elif current_step == CalibrationStep.START_CAMERA_CALIBRATION:
                self._start_camera_calibration(gcmd)
                state['step'] = CalibrationStep.WAIT_CAMERA_CALIBRATION

            elif current_step == CalibrationStep.WAIT_CAMERA_CALIBRATION:
                if self.calibration_data.transform_matrix is not None:
                    state['camera_calibrated'] = True
                    state['step'] = CalibrationStep.START_T0_NOZZLE_CALIBRATION

            elif current_step == CalibrationStep.START_T0_NOZZLE_CALIBRATION:
                self._start_nozzle_calibration(gcmd)
                state['step'] = CalibrationStep.WAIT_T0_NOZZLE_CALIBRATION

            elif current_step == CalibrationStep.WAIT_T0_NOZZLE_CALIBRATION:
                if self.current_state != CalibrationState.NOZZLE_CALIBRATION:
                    state['step'] = CalibrationStep.SET_ORIGIN_AND_SWITCH_TOOL

            elif current_step == CalibrationStep.SET_ORIGIN_AND_SWITCH_TOOL:
                self.cmd_KTAMV_SET_ORIGIN(gcmd)
                self._switch_tool(1)
                state['step'] = CalibrationStep.MOVE_TO_CENTER_T1

            elif current_step == CalibrationStep.MOVE_TO_CENTER_T1:
                if self.center_position:
                    self.pm.moveAbsolute(
                        X=self.center_position[0],
                        Y=self.center_position[1])
                else:
                    self._move_to_camera_center()
                state['step'] = CalibrationStep.START_T1_NOZZLE_CALIBRATION

            elif current_step == CalibrationStep.START_T1_NOZZLE_CALIBRATION:
                self._start_nozzle_calibration(gcmd)
                state['step'] = CalibrationStep.WAIT_T1_NOZZLE_CALIBRATION

            elif current_step == CalibrationStep.WAIT_T1_NOZZLE_CALIBRATION:
                if self.current_state != CalibrationState.NOZZLE_CALIBRATION:
                    state['step'] = CalibrationStep.CALCULATE_AND_SAVE_OFFSET

            elif current_step == CalibrationStep.CALCULATE_AND_SAVE_OFFSET:
                self.cmd_KTAMV_GET_OFFSET(gcmd)
                state['step'] = CalibrationStep.COMPLETE

            elif current_step == CalibrationStep.COMPLETE:
                gcmd.respond_info("Nozzle calibration completed successfully!")
                self.current_state = CalibrationState.IDLE

            if current_step != CalibrationStep.COMPLETE:
                self.reactor.register_callback(self._polling_step, eventtime + interval)
        except Exception as e:
            state['error'] = str(e)
            self.current_state = CalibrationState.IDLE
            raise self.gcode.error(f"Nozzle calibration failed: {str(e)}")

    def _start_camera_calibration(self, gcmd):
        try:
            self.pm.ensureHomed()
            self._switch_tool(0)
            self._move_to_camera_center()

            self.camera_calibration = CameraCalibrationSession()
            self.camera_calibration.start_xy = self.pm.get_gcode_position()[:2]

            self._init_search()

            self.calibration_data.clear()
            self.current_state = CalibrationState.CAMERA_CALIBRATION
            self._calib_phase = 'searching'
            self.gcmd = gcmd
            self._call_remote_method("get_nozzle_position")
        except Exception as e:
            self._handle_operation_failure(f"Camera calibration failed: {str(e)}")

    def _search_next_calibration_point(self):
        self._search_next_point('_calib_phase')

    def _move_to_calibration_point(self, index):
        try:
            if not self.camera_calibration or index >= len(self.camera_calibration.points):
                logging.error(f"Invalid calibration point index: {index}")
                return

            dx, dy = self.camera_calibration.points[index]

            # Check axis bounds before moving
            current_xy = self.pm.get_gcode_position()[:2]
            target_x = current_xy[0] + dx
            target_y = current_xy[1] + dy
            curtime = self.printer.get_reactor().monotonic()
            toolhead = self.printer.lookup_object("toolhead")
            kin_status = toolhead.get_kinematics().get_status(curtime)
            x_min = float(kin_status['axis_minimum'].x)
            x_max = float(kin_status['axis_maximum'].x)
            y_min = float(kin_status['axis_minimum'].y)
            y_max = float(kin_status['axis_maximum'].y)
            if (target_x < x_min or target_x > x_max
                    or target_y < y_min or target_y > y_max):
                self.gcmd.respond_info(
                    f"Skipping calibration point {index + 1}"
                    f" ({dx},{dy}) - out of bounds")
                self.camera_calibration.current_index += 1
                if self.camera_calibration.current_index < len(self.camera_calibration.points):
                    self._move_to_calibration_point(
                        self.camera_calibration.current_index)
                else:
                    self._finish_camera_calibration()
                return

            logging.info(f"Moving to calibration point {index + 1}: relative move X{dx} Y{dy}")
            self._with_slow_speed(lambda: self.pm.moveRelative(X=dx, Y=dy))

            self.reactor.register_callback(
                lambda e: self._call_remote_method("get_nozzle_position"),
                self.reactor.monotonic() + 0.2
            )
        except Exception as e:
            self._handle_operation_failure(f"Movement error: {str(e)}")

    def _finish_camera_calibration(self):
        try:
            current = self.pm.get_gcode_position()[:2]
            dx_back = self.camera_calibration.start_xy[0] - current[0]
            dy_back = self.camera_calibration.start_xy[1] - current[1]

            if abs(dx_back) > 0.01 or abs(dy_back) > 0.01:
                self.gcode.respond_info("Moving back to starting position")
                self.pm.moveRelative(X=dx_back, Y=dy_back)

            total_points = len(self.camera_calibration.points)
            valid_points = len(self.calibration_data.points)

            if valid_points < total_points * 0.75:
                raise Exception(f"Only {valid_points}/{total_points} points succeeded (<75%)")

            result = self.utl.get_average_mpp(
                self.calibration_data.mm_per_pixels,
                self.calibration_data.space_coordinates,
                self.calibration_data.camera_coordinates,
                self.gcmd
            )
            if result:
                avg_mpp, _, _, _ = result
                self.calibration_data.average_mpp = avg_mpp

            self._call_remote_method(
                "calculate_camera_to_space_matrix",
                calibration_points=self.calibration_data.transform_input
            )

        except Exception as e:
            self._handle_operation_failure(f"Camera calibration failed: {str(e)}")

    def _start_nozzle_calibration(self, gcmd):
        try:
            self.pm.ensureHomed(home=False)

            if self.calibration_data.transform_matrix is None:
                raise self.gcode.error("Camera is not calibrated, aborting")
            if (not hasattr(self.calibration_data.transform_matrix, 'shape') or 
                self.calibration_data.transform_matrix.shape[0] < 2):
                raise self.gcode.error("Camera calibration matrix is invalid")

            self.current_state = CalibrationState.NOZZLE_CALIBRATION
            self.operation_retries = 0
            self.gcmd = gcmd
            self._call_remote_method("get_nozzle_position")

        except Exception as e:
            self._handle_operation_failure(f"Nozzle calibration failed: {str(e)}")

    def _calibrate_nozzle_offset(self, nozzle_uv, nozzle_xy):
        try:
            cx, cy = self.utl.normalize_coords(nozzle_uv)
            calibration_vector = [cx**2, cy**2, cx * cy, cx, cy, 0]
            if self.calibration_data.transform_matrix is not None:
                offsets = -self.adjusted_gain * (self.calibration_data.transform_matrix @ calibration_vector)
                offsets = [round(x, 3) for x in offsets]
                if self.adjusted_gain > 0.48:
                    self.adjusted_gain -= 0.01

                self.gcmd.respond_info(
                    f"Nozzle calibration gain {self.adjusted_gain:.2f} attempt {self.operation_retries + 1}:\n"
                    f"Position: X{nozzle_xy[0]:.2f} Y{nozzle_xy[1]:.2f}\n"
                    f"UV: {nozzle_uv}\n"
                    f"Offset: X{offsets[0]:.2f} Y{offsets[1]:.2f}"
                )

                if abs(offsets[0]) < 0.005 and abs(offsets[1]) < 0.005:
                    self.current_state = CalibrationState.IDLE
                    self.adjusted_gain = self.gain
                    return
                pixel_offsets = [
                    offsets[0] / self.calibration_data.average_mpp,
                    offsets[1] / self.calibration_data.average_mpp
                ]

                new_uv = [
                    nozzle_uv[0] + pixel_offsets[0],
                    nozzle_uv[1] + pixel_offsets[1]
                ]

                if (new_uv[0] > FRAME_WIDTH or new_uv[0] < 0 or
                    new_uv[1] > FRAME_HEIGHT or new_uv[1] < 0):
                    self._retry_camera_calibration("Calibration would move nozzle outside camera frame")
                    return
                self._with_slow_speed(
                    lambda: self.pm.moveRelative(X=offsets[0], Y=offsets[1]))
                self.operation_retries += 1
                if self.operation_retries >= self.max_retries:
                    self._retry_camera_calibration("Nozzle calibration reached maximum retries")
                    return
                self.reactor.register_callback(
                    lambda e: self._call_remote_method("get_nozzle_position"),
                    self.reactor.monotonic() + 0.5
                )

        except Exception as e:
            self._handle_operation_failure(f"Nozzle offset calibration failed: {str(e)}")

    def _handle_nozzle_not_found(self):
        if self.operation_retries >= self.max_retries:
            self._retry_camera_calibration("Nozzle not found after maximum retries")
            return

        wiggle_patterns = [(0.3, 0), (-0.5, 0), (0.3, 0.3), (0, -0.5)]
        wiggle_index = self.operation_retries % len(wiggle_patterns)
        dx, dy = wiggle_patterns[wiggle_index]

        self.pm.moveRelative(X=dx, Y=dy)
        self.operation_retries += 1

        self.reactor.register_callback(
            lambda e: self._call_remote_method("get_nozzle_position"),
            self.reactor.monotonic() + 0.5
        )

    def _start_simple_position_detection(self, gcmd):
        try:
            if not self._ensure_homed(home=False):
                self._ensure_homed()
                self._move_to_camera_center()
            self.current_state = CalibrationState.SIMPLE_POSITION
            self._simple_position_gcmd = gcmd
            self._call_remote_method("get_nozzle_position")

        except Exception as e:
            self._handle_operation_failure(f"Simple position detection failed: {str(e)}")

    def _call_remote_method(self, method, **kwargs):
        webhooks = self.printer.lookup_object('webhooks')
        try:
            webhooks.call_remote_method(method, **kwargs)
        except Exception as e:
            logging.error(f"Remote method {method} failed: {str(e)}")
            raise

    def _ensure_homed(self, home=True) -> bool:
        return self.pm.ensureHomed(home)

    def cmd_KTAMV_MOVE_DATUM_CENTER(self, gcmd):
        use_offset = gcmd.get_int('USE_OFFSET', default=1) != 0
        self._ensure_homed()
        self._switch_tool(0)
        if use_offset:
            self._move_to_camera_center()
        else:
            self.pm.moveAbsoluteToArray(self.camera_center_points)

    def cmd_KTAMV_CLEAN_NOZZLE(self, gcmd):
        self.clean_nozzle()
        self._move_to_camera_center()

    def cmd_KTAMV_SET_CENTER_OFFSET(self, gcmd):
        toolhead = self.printer.lookup_object("toolhead")
        pos = toolhead.get_position()
        x_offset = round(pos[0] - self.camera_center_points[0], 3)
        y_offset = round(pos[1] - self.camera_center_points[1], 3)
        z_offset = round(pos[2] - self.camera_center_points[2], 3)

        for var_name, value in [
            ("camera_x_offset_val", x_offset),
            ("camera_y_offset_val", y_offset),
            ("camera_z_offset_val", z_offset)
        ]:
            script = f'SAVE_VARIABLE VARIABLE={var_name} VALUE=\"{value}\"'
            self.gcode.run_script_from_command(script)

    def clean_nozzle(self):
        nozzle_cleaner = self.printer.lookup_object('nozzle_cleaner', None)
        if nozzle_cleaner is not None:
            script = "CLEAN_NOZZLE"
            self.gcode.run_script_from_command(script)
            toolhead = self.printer.lookup_object("toolhead")
            toolhead.wait_moves()
        else:
            logging.info("Nozzle cleaner not configured, skipping cleaning step.")

    def _move_to_camera_center(self):
        camera_canter = list(self.camera_center_points)
        save_variables = self.printer.lookup_object('save_variables').allVariables
        if "camera_x_offset_val" in save_variables:
            camera_canter[0] = self.camera_center_points[0] + save_variables.get("camera_x_offset_val", 0.)
        if "camera_y_offset_val" in save_variables:
            camera_canter[1] = self.camera_center_points[1] + save_variables.get("camera_y_offset_val", 0.)
        if "camera_z_offset_val" in save_variables:
            camera_canter[2] = self.camera_center_points[2] + save_variables.get("camera_z_offset_val", 0.)
        self.pm.moveAbsoluteToArray(camera_canter)

    def _switch_tool(self, tool_index):
        self.gcode.run_script_from_command(f"T{tool_index}")

    def _handle_operation_failure(self, error_msg):
        self._cleanup_operation()
        self.current_state = CalibrationState.IDLE
        if hasattr(self, 'calibration_state'):
            self.calibration_state['error'] = error_msg
        self.gcode.respond_raw(f'!! {error_msg}')
        raise self.gcode.error(f"{error_msg}")
    
    def _retry_camera_calibration(self, retry_message):
        """Retry camera calibration with proper state reset"""
        self.calibration_retries += 1
        if self.calibration_retries > self.max_calibration_retries:
            self.calibration_retries = 0
            self._handle_operation_failure(f"{retry_message} after all calibration attempts")
        
        # Reset calibration to camera calibration step
        self.gcode.respond_info(
            f"{retry_message}. Retrying camera calibration ({self.calibration_retries}/{self.max_calibration_retries})..."
        )
        self.current_state = CalibrationState.IDLE
        self._cleanup_operation()
        
        # Reset calibration state to camera calibration step
        if hasattr(self, 'calibration_state'):
            self.calibration_state['step'] = CalibrationStep.START_CAMERA_CALIBRATION
            self.calibration_state['camera_calibrated'] = False
            self.calibration_data.clear()
        return

    def _cleanup_operation(self):
        self.operation_retries = 0
        self.adjusted_gain = self.gain
        if self.camera_calibration:
            self.camera_calibration = None

    def get_status(self, eventtime=None):
        status = {
            "current_state": self.current_state.value,
            "is_calibrated": self.calibration_data.transform_matrix is not None,
            "average_mpp": self.calibration_data.average_mpp,
            "center_position": self.center_position,
            "calibration_points": len(self.calibration_data.points),
            "travel_speed": self.pm.speed if self.pm else 0,
            "calibration_status": self.calibration_status
        }
        return status

class Ktamv_Utl:
    def __init__(self):
        pass

    def get_average_mpp(self,
        mpps: list, space_coordinates: list, camera_coordinates: list, gcmd
    ):
        try:
            original_count = len(mpps)
            
            if original_count < 3:
                raise ValueError(f"Need at least 3 points, current: {original_count}")
            
            mpps_np = np.array(mpps)

            median = np.median(mpps_np)
            mad = np.median(np.abs(mpps_np - median))

            sigma_estimate = mad / 0.6745 if mad > 0 else 0

            threshold = 3 * sigma_estimate if sigma_estimate > 0 else 0

            valid_indices = []
            filtered_mpps = []
            filtered_space = []
            filtered_camera = []
            
            for i, mpp in enumerate(mpps):
                if abs(mpp - median) <= threshold:
                    valid_indices.append(i)
                    filtered_mpps.append(mpp)
                    filtered_space.append(space_coordinates[i])
                    filtered_camera.append(camera_coordinates[i])
            if len(filtered_mpps) < 3:
                raise ValueError(f"Insufficient points after filtering: {len(filtered_mpps)}/{original_count}")
            weighted_mpps = []
            weights = []
            
            for i in range(len(filtered_mpps)):
                distances = []
                for j in range(len(filtered_mpps)):
                    if i != j:
                        dx = filtered_space[j][0] - filtered_space[i][0]
                        dy = filtered_space[j][1] - filtered_space[i][1]
                        distances.append(np.sqrt(dx**2 + dy**2))
                
                weight = np.mean(distances) if distances else 1
                weights.append(weight)
                weighted_mpps.append(filtered_mpps[i] * weight)
            
            if sum(weights) > 0:
                weighted_mean = sum(weighted_mpps) / sum(weights)
            else:
                weighted_mean = np.mean(filtered_mpps)
            
            std_dev = np.std(filtered_mpps)
            cv = std_dev / weighted_mean
            
            if cv > 0.15:
                gcmd.respond_info("Warning: Coefficient of variation exceeds 15%, re-calibration recommended")
            
            return weighted_mean, filtered_mpps, filtered_space, filtered_camera
            
        except Exception as e:
            logging.error(f"Error in get_average_mpp_simple: {str(e)}")
            raise

    @staticmethod
    def _get_std_dev_and_mean(mpps: list):
        # Calculate the average mm per pixel and the standard deviation
        mpps_std_dev = stdev(mpps)
        mpp = round(mean(mpps), 4)
        return mpps_std_dev, mpp

    @staticmethod
    def normalize_coords(coords: list[float]) -> tuple[float, float]:
        # Use module-level constants if parameters are not provided
        xdim = FRAME_WIDTH
        ydim = FRAME_HEIGHT
        norm_x = coords[0] / xdim - 0.5
        norm_y = coords[1] / ydim - 0.5
        return (norm_x, norm_y)

def load_config(config):
    return Ktamv(config)

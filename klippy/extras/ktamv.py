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

    def ensureHomed(self, home=True) -> bool:
        curtime = self.printer.get_reactor().monotonic()
        kin_status = self.toolhead.get_kinematics().get_status(curtime)
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

    def moveRelativeToArray(self, pos_array, protected=False):
        self.moveRelative(
            pos_array[0], pos_array[1], pos_array[2], protected
        )

    def complexMoveRelative(self, X=0, Y=0, Z=0):
        self.moveRelative(X, Y, Z, True)

    def moveAbsoluteToArray(self, pos_array):
        gcode = "G90\nG1 "
        for i in range(len(pos_array)):
            if i == 0:
                gcode += "X%s " % (pos_array[i])
            elif i == 1:
                gcode += "Y%s " % (pos_array[i])
            elif i == 2:
                gcode += "Z%s " % (pos_array[i])
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
            [0, -1.0], [1.0, 0], [0, 1.0], [0, 0.1], [-1, 0],
            [-1.0, 0], [0, -1.0], [0, -1.0], [1.0, 0], [0, 0.8],
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
        self.max_retries = 30
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
            self._handle_operation_failure(error_msg)
            return
        state_handlers = {
            CalibrationState.CAMERA_CALIBRATION: self._process_calibration_position,
            CalibrationState.NOZZLE_CALIBRATION: self._process_nozzle_calibration_position,
            CalibrationState.SIMPLE_POSITION: self._process_simple_position,
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

        if not self.camera_calibration.initial_position_received:
            if position_data is not None:
                self.camera_calibration.start_uv = position_data
                self.camera_calibration.initial_position_received = True
                self.gcode.respond_info("starting calibration moves")
                self._move_to_calibration_point(0)
            else:
                self._handle_operation_failure("Failed to get initial nozzle position for calibration")
            return

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
                self.gcode.respond_info(
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

    cmd_KTAMV_CALIB_NOZZLE_help = (
        "Calibrates the movement of the active nozzle"
        + " around the point it started at"
    )
    def cmd_KTAMV_CALIB_NOZZLE(self, gcmd):
        gcmd.respond_info("Starting nozzle calibration")
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
        "Calibrates the movement of the active camera"
        + " around the point it started at")
    def cmd_KTAMV_CALIB_CAMERA(self, gcmd):
        self.gcode.respond_info("Starting mm/px calibration")
        self._start_camera_calibration(gcmd)

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

            self.calibration_data.clear()
            self.current_state = CalibrationState.CAMERA_CALIBRATION
            self.gcmd = gcmd
            self._call_remote_method("get_nozzle_position") 
        except Exception as e:
            self._handle_operation_failure(f"Camera calibration failed: {str(e)}")

    def _move_to_calibration_point(self, index):
        try:
            if not self.camera_calibration or index >= len(self.camera_calibration.points):
                logging.error(f"Invalid calibration point index: {index}")
                return

            dx, dy = self.camera_calibration.points[index]
            logging.info(f"Moving to calibration point {index + 1}: relative move X{dx} Y{dy}")
            original_speed = self.pm.speed
            self.pm.speed = 500
            try:
                self.pm.moveRelative(X=dx, Y=dy)
            finally:
                self.pm.speed = original_speed

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
                if self.adjusted_gain > 0.4:
                    self.adjusted_gain -= 0.03

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
                    raise self.gcmd.error(
                        "Calibration would move nozzle outside camera frame. "
                        "This is likely due to bad mm/px calibration."
                    )
                original_speed = self.pm.speed
                try:
                    self.pm.speed = 500
                    self.pm.moveRelative(X=offsets[0], Y=offsets[1])
                finally:
                    self.pm.speed = original_speed
                self.operation_retries += 1
                if self.operation_retries >= self.max_retries:
                    self._handle_operation_failure("Nozzle calibration reached maximum retries")
                self.reactor.register_callback(
                    lambda e: self._call_remote_method("get_nozzle_position"),
                    self.reactor.monotonic() + 0.5
                )

        except Exception as e:
            self._handle_operation_failure(f"Nozzle offset calibration failed: {str(e)}")

    def _handle_nozzle_not_found(self):
        if self.operation_retries >= self.max_retries:
            self._handle_operation_failure("Nozzle not found after maximum retries")

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
            # Save initial mpps for later comparison
            initial_mpps = mpps.copy()

            # Calculate the average mm per pixel and the standard deviation
            mpps_std_dev, mpp = self._get_std_dev_and_mean(mpps)

            # Exclude the highest value if it deviates more than 20% from the mean
            if max(mpps) > mpp + (mpp * 0.20):
                __max_index = mpps.index(max(mpps))
                mpps.pop(__max_index)
                space_coordinates.pop(__max_index)
                camera_coordinates.pop(__max_index)

            # Calculate the average mm per pixel and the standard deviation
            mpps_std_dev, mpp = self._get_std_dev_and_mean(mpps)

            # Exclude the lowest value if it deviates more than 20% from the mean
            if min(mpps) < mpp - (mpp * 0.20):
                __min_index = mpps.index(min(mpps))
                mpps.pop(__min_index)
                space_coordinates.pop(__min_index)
                camera_coordinates.pop(__min_index)

            # Calculate the average mm per pixel and the standard deviation
            mpps_std_dev, mpp = self._get_std_dev_and_mean(mpps)

            # Exclude values more than 2 standard deviations from mean
            for i in reversed(range(len(mpps))):
                if mpps[i] > mpp + (mpps_std_dev * 2) or mpps[i] < mpp - (mpps_std_dev * 2):
                    mpps.pop(i)
                    space_coordinates.pop(i)
                    camera_coordinates.pop(i)

            # Calculate the average mm per pixel and the standard deviation
            mpps_std_dev, mpp = self._get_std_dev_and_mean(mpps)

            # Exclude any other value that deviates more than 25% from mean value
            for i in reversed(range(len(mpps))):
                if mpps[i] > mpp + (mpp * 0.25) or mpps[i] < mpp - (mpp * 0.25):
                    mpps.pop(i)

            # Calculate the average mm per pixel and the standard deviation
            mpps_std_dev, mpp = self._get_std_dev_and_mean(mpps)

            # Final check if standard deviation is still too high
            gcmd.respond_info(
                (
                    "Final mm/pixel is %.4f with a std. dev. of %.1f"
                    % (mpp, (mpps_std_dev / mpp) * 100)
                )
                + "%" + (
                    ".\n Final mm/px is calculated from %d of %d values"
                    % (len(mpps), len(initial_mpps))
                )
            )

            if mpps_std_dev / mpp > 0.2:
                gcmd.respond_info(
                    "Standard deviation is still too high. Calibration failed."
                )
                return None
            return mpp, mpps, space_coordinates, camera_coordinates
        except Exception as e:
            logging.error(f"Error in get_average_mpp: {str(e)}")
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

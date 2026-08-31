from enum import Enum, auto
import logging

from . import probe


class NozzlesZCalibrationState(Enum):
    IDLE = auto()
    RUNNING = auto()
    FINISHED = auto()
    ABORTED = auto()


class NozzlesZCalibrator:
    def __init__(self, config, nozzles_probe):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.nozzles_probe = nozzles_probe
        self.probe_session = nozzles_probe.probe_session
        self.name = config.get_name().split(' ')[-1]
        self.state = NozzlesZCalibrationState.IDLE
        self.running = False
        self.endstop_relative_position = list(config.getfloatlist(
            'endstop_relative_position', count=2))
        self.endstop_position = list(self.endstop_relative_position)
        self.sample_count = config.getint('sample_count', 3, minval=1)
        self.horizontal_move_z = config.getfloat(
            'horizontal_move_z', 10.0, above=0.)
        self.probe_z_min = config.getfloat('probe_z_min', -5.0)
        self.move_speed = config.getfloat('move_speed', 200.0, above=0.)
        self.settle_time = config.getfloat('settle_time', 1.0, minval=0.0)
        self.max_z_offset = config.getfloat(
            'max_z_offset', 1.0, minval=0.0)
        self.save_variable = config.get(
            'save_variable', 'nozzle_z_offset_val')
        self.auto_save = config.getboolean('auto_save', True)
        self.x_offset = 0.0
        self.y_offset = 0.0
        self.last_offset = None
        self._saved_z_limits = None
        self.gcode.register_command(
            'NOZZLES_Z_CALIBRATE',
            self.cmd_NOZZLES_Z_CALIBRATE,
            desc="Calibrate nozzles Z offset")

    def get_status(self, eventtime):
        return {
            'state': self.state.name.lower(),
            'running': self.running,
            'offset': self.last_offset,
            'endstop_position': self.endstop_position,
            'probe_z_min': self.probe_z_min,
        }

    def _toolhead(self):
        return self.printer.lookup_object('toolhead')

    def _update_offset(self):
        ktamv = self.printer.lookup_object('ktamv')
        camera_center = ktamv.camera_center_points
        save_variables = self.printer.lookup_object('save_variables').allVariables
        self.x_offset = save_variables["nozzle_x_offset_val"]
        self.y_offset = save_variables["nozzle_y_offset_val"]
        self.endstop_position[0] = (camera_center[0]
                                    + self.endstop_relative_position[0]
                                    + save_variables.get('camera_x_offset_val', 0.))
        self.endstop_position[1] = (camera_center[1]
                                    + self.endstop_relative_position[1]
                                    + save_variables.get('camera_y_offset_val', 0.))

    def _set_temporary_z_limit(self, probe_z_min):
        kin = self._toolhead().get_kinematics()
        limits = getattr(kin, 'limits', None)
        if limits is None or len(limits) < 3:
            raise self.printer.command_error(
                "Current kinematics do not support temporary Z limit changes")
        z_limits = limits[2]
        if z_limits[0] <= probe_z_min:
            self._saved_z_limits = None
            return
        self._saved_z_limits = z_limits
        limits[2] = (probe_z_min, z_limits[1])

    def _restore_z_limit(self):
        if self._saved_z_limits is None:
            return
        kin = self._toolhead().get_kinematics()
        kin.limits[2] = self._saved_z_limits
        self._saved_z_limits = None

    def _run_tool_command(self, command):
        if command.strip():
            self.gcode.run_script_from_command(command)

    def _settle(self):
        if self.settle_time > 0.0:
            self._toolhead().dwell(self.settle_time)

    def _move_safe_z(self, z, speed):
        self._toolhead().manual_move([None, None, z], speed)

    def _move_xy(self, x, y, speed, tool=0):
        if tool == 1:
            x += self.x_offset
            y += self.y_offset
        self._toolhead().manual_move([x, y, None], speed)

    def _run_single_probe(self, gcmd, label, sample_count):
        self.probe_session.z_position = self.probe_z_min
        fo_params = dict(gcmd.get_command_parameters())
        fo_params['SAMPLES'] = str(sample_count)
        gcode = self.printer.lookup_object('gcode')
        fo_gcmd = gcode.create_gcode_command("", "", fo_params)
        self.probe_session.run_probe(fo_gcmd)
        positions = self.probe_session.pull_probed_results()
        if not positions:
            raise gcmd.error("%s probe did not return a result" % (label,))
        result = float(positions[-1][2])
        gcmd.respond_info("%s trigger Z: %.6f" % (label, result))
        return result

    def _probe_nozzle(self, gcmd, tool_command, label,
                     move_speed, lift_speed, sample_count, tool=0):
        self._run_tool_command(tool_command)
        self._settle()
        self._move_safe_z(self.horizontal_move_z, lift_speed)
        self._move_xy(self.endstop_position[0], self.endstop_position[1],
                      move_speed, tool=tool)
        self._settle()
        z = self._run_single_probe(gcmd, label, sample_count)
        self._move_safe_z(self.horizontal_move_z, lift_speed)
        return z

    def _save_result(self, gcmd, offset):
        save_variables = self.printer.lookup_object('save_variables', None)
        if save_variables is None:
            raise gcmd.error(
                "save_variables module is required to save the result")
        script = "SAVE_VARIABLE VARIABLE=%s VALUE=%.3f" % (
            self.save_variable, offset)
        self.gcode.run_script_from_command(script)

    cmd_NOZZLES_Z_CALIBRATE_help = "Calibrate nozzles Z offset"

    def _clean_nozzle(self):
        nozzle_cleaner = self.printer.lookup_object('nozzle_cleaner', None)
        if nozzle_cleaner is not None:
            script = "CLEAN_NOZZLE"
            self.gcode.run_script_from_command(script)
            toolhead = self.printer.lookup_object("toolhead")
            toolhead.wait_moves()
        else:
            logging.info("Nozzle cleaner not configured, skipping cleaning step.")

    def cmd_NOZZLES_Z_CALIBRATE(self, gcmd):
        if self.running:
            gcmd.respond_info(
                "A calibration process is already running. Please complete "
                "it first or restart the printer")
            return
        self._update_offset()
        params = self.probe_session.get_probe_params(gcmd)
        z_speed = params['lift_speed']
        need_clean = gcmd.get_int('CLEAN', 1, minval=0, maxval=1)
        move_speed = gcmd.get_float('MOVE_SPEED', self.move_speed, above=0.)
        sample_count = gcmd.get_int("SAMPLES", self.sample_count, minval=1)
        should_save = bool(gcmd.get_int('SAVE', int(self.auto_save), minval=0, maxval=1))
        session_open = False
        self.running = True
        self.last_offset = None
        old_probe_z = self.probe_session.z_position
        self.state = NozzlesZCalibrationState.RUNNING

        try:
            gcmd.respond_info(
                "Starting nozzles Z calibration at X:%.3f Y:%.3f, "
                "safe Z:%.3f, probe Z min:%.3f"
                % (self.endstop_position[0], self.endstop_position[1],
                   self.horizontal_move_z, self.probe_z_min))

            self.gcode.run_script_from_command("G28")
            if need_clean:
                self._clean_nozzle()
            self._set_temporary_z_limit(self.probe_z_min)
            self.probe_session.start_probe_session(gcmd)
            session_open = True

            left_z = self._probe_nozzle(
                gcmd, "T0", "Left nozzle", move_speed, z_speed, sample_count)
            right_z = self._probe_nozzle(
                gcmd, "T1", "Right nozzle", move_speed, z_speed, sample_count, tool=1)

            final_offset = right_z - left_z
            self.last_offset = final_offset
            report = (
                "Left nozzle trigger Z:  %.6f\n"
                "Right nozzle trigger Z: %.6f\n"
                "Final nozzles Z offset: %.6f\n"
                % (left_z, right_z, final_offset))
            if (self.max_z_offset > 0.0
                    and not (-self.max_z_offset <= final_offset
                             <= self.max_z_offset)):
                report += (
                    "Final offset %.3f is out of range "
                    "[-%.3f, %.3f].\n"
                    % (final_offset, self.max_z_offset, self.max_z_offset))
                if should_save:
                    report += "Result was not saved.\n"
                should_save = False
            if should_save:
                self._save_result(gcmd, final_offset)
                report += "Saved to %s\n" % (self.save_variable,)
            gcmd.respond_info(report)
            self.state = NozzlesZCalibrationState.FINISHED
        except Exception:
            self.state = NozzlesZCalibrationState.ABORTED
            raise
        finally:
            try:
                if session_open or self._saved_z_limits is not None:
                    self._move_safe_z(self.horizontal_move_z, z_speed)
            except Exception:
                logging.exception("Failed to move back to safe Z")
            try:
                if session_open:
                    self.probe_session.end_probe_session()
            except Exception:
                logging.exception("Failed to end probe session")
            try:
                self._restore_z_limit()
            except Exception:
                logging.exception("Failed to restore temporary Z limits")
            self.probe_session.z_position = old_probe_z
            self.running = False


class NozzlesZProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.probe_name = config.get_name().split(' ')[-1]
        self.printer.add_object(self.probe_name, self)
        self.mcu_probe = probe.ProbeEndstopWrapper(config)
        self.probe_session = probe.ProbeSessionHelper(config, self.mcu_probe)
        self.mcu_probe.probe_session = self.probe_session
        self.calibrator = NozzlesZCalibrator(config, self)
        query_endstops = self.printer.load_object(config, 'query_endstops')
        query_endstops.register_endstop(
            self.mcu_probe.mcu_endstop, self.probe_name)

    def get_probe_params(self, gcmd=None):
        return self.probe_session.get_probe_params(gcmd)

    def get_status(self, eventtime):
        return self.calibrator.get_status(eventtime)

    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)


def load_config(config):
    return NozzlesZProbe(config)

from enum import Enum, auto
from . import probe
import logging

# Helper to implement common probing commands
class OffsetProbeCommandHelper(probe.ProbeCommandHelper):
    def __init__(self, config, probe, query_endstop=None):
        self.printer = config.get_printer()
        self.probe = probe
        self.query_endstop = query_endstop
        self.last_state = False
        self.z_offset = self.avg_value = 0.
        self.last_z_result = 0.
        self.probe_calibrate_z = 0.
        self.name = config.get_name()
        self.register_commands()

    def register_commands(self):
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        # QUERY_PROBE command
        gcode.register_command('QUERY_PROBE_NOZZLE',
                                    self.cmd_QUERY_PROBE,
                                    desc=self.cmd_QUERY_PROBE_help)
        # PROBE command
        gcode.register_command('PROBE_NOZZLE',
                                    self.cmd_PROBE,
                                    desc=self.cmd_PROBE_help)
        # PROBE_CALIBRATE command
        gcode.register_command('PROBE_CALIBRATE_NOZZLE',
                                    self.cmd_PROBE_CALIBRATE,
                                    desc=self.cmd_PROBE_CALIBRATE_help)
        # Other commands
        gcode.register_command('PROBE_ACCURACY_NOZZLE',
                                   self.cmd_PROBE_ACCURACY,
                               desc=self.cmd_PROBE_ACCURACY_help)
        gcode.register_command('Z_OFFSET_APPLY_PROBE_NOZZLE',
                               self.cmd_Z_OFFSET_APPLY_PROBE,
                               desc=self.cmd_Z_OFFSET_APPLY_PROBE_help)

class CalibrationState(Enum):
    INIT = auto()
    LEFT_PROBE_ACCURACY = auto()
    RIGHT_PROBE_ACCURACY = auto()
    FINISHED = auto()
    ABORTED = auto()

class DualProbeCalibrator:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split(' ')[-1]
        self.data = {
            'left_probe_avg': None,
            'right_probe_avg': None
        }
        self.state = CalibrationState.INIT
        self._waiting = False
        self.next_callback = None
        self.x_offset = self.y_offset = 0
        self.base_x_offset = config.getfloat('base_x_offset', 0.)
        self.max_z_offset = config.getfloat('max_z_offset', 0.)
        self.gcode.register_command(
            'DUAL_PROBE_CALIBRATE',
            self.cmd_DUAL_PROBE_CALIBRATE,
            desc="Automatically calibrate dual-nozzle offset"
        )

    def cmd_DUAL_PROBE_CALIBRATE(self, gcmd):
        if self.state != CalibrationState.INIT:
            gcmd.respond_info("A calibration process is already running. Please complete it first or restart the printer")
            return
        self.gcmd = gcmd
        self._get_offset_var()
        self.state = CalibrationState.LEFT_PROBE_ACCURACY
        self._start_next_step()

    def _get_offset_var(self):
        save_variables = self.printer.lookup_object('save_variables').allVariables
        missing_vars = []
        if "nozzle_x_offset_val" not in save_variables:
            missing_vars.append("nozzle_x_offset_val")
        if "nozzle_y_offset_val" not in save_variables:
            missing_vars.append("nozzle_y_offset_val")
        if missing_vars:
            self.gcmd.respond_info(f"Missing calibration data: {', '.join(missing_vars)}. Please complete XY offset calibration first.")
            return
        self.x_offset = self.base_x_offset - save_variables["nozzle_x_offset_val"]
        self.y_offset = save_variables["nozzle_y_offset_val"]
        self.z_calibrate_compensation = save_variables.get("nozzle_z_offset_compensation", 0.)

    def _start_next_step(self):
        if self._waiting:
            logging.warning("Waiting for user operation to complete, skipping duplicate execution")
            return
        current_state = self.state
        if current_state == CalibrationState.LEFT_PROBE_ACCURACY:
            self.gcmd.respond_info("Starting left nozzle z_offset calibration...")
            self._run_automatic_gcode(
                "G28\n"
                "G4 P1000\n"
                "PROBE_ACCURACY",
                self._on_left_accuracy_done
            )
        elif current_state == CalibrationState.RIGHT_PROBE_ACCURACY:
            self.gcmd.respond_info("Obtaining right nozzle probe average value...")
            self._run_automatic_gcode(
                "T1\n"
                f"_CLIENT_LINEAR_MOVE X={-self.x_offset} Y={-self.y_offset} F=6000\n"
                "G4 P1000\n"
                "PROBE_ACCURACY_NOZZLE",
                self._on_right_accuracy_done
            )
        elif current_state == CalibrationState.FINISHED:
            self._calculate_final_offset()
        elif current_state == CalibrationState.ABORTED:
            self.gcmd.respond_info("Calibration process has been aborted")
            self._reset_state()

    def _run_automatic_gcode(self, cmd, callback):
        self.next_callback = callback
        try:
            self.gcode.run_script_from_command(cmd)
        except Exception as e:
            self.gcmd.respond_info(f"Gcode Command Error")
        finally:
            self._on_automatic_gcode_done()

    def _on_automatic_gcode_done(self):
        if self.next_callback:
            self.next_callback()
        self.next_callback = None
        self._advance_state()
        self._start_next_step()

    def _advance_state(self):
        current = self.state
        if current == CalibrationState.LEFT_PROBE_ACCURACY:
            self.state = CalibrationState.RIGHT_PROBE_ACCURACY
        elif current == CalibrationState.RIGHT_PROBE_ACCURACY:
            self.state = CalibrationState.FINISHED

    def _on_left_accuracy_done(self):
        left_avg = self._get_probe_accuracy_avg("probe")
        self.data['left_probe_avg'] = left_avg
        self.gcmd.respond_info(f"Lift nozzle probe average: {left_avg:.6f}")

    def _on_right_accuracy_done(self):
        right_avg = self._get_probe_accuracy_avg(self.name)
        self.data['right_probe_avg'] = right_avg
        self.gcmd.respond_info(f"Right nozzle probe average: {right_avg:.6f}")

    def _calculate_final_offset(self):
        output = ""
        try:
            left_avg = self.data['left_probe_avg']
            right_avg = self.data['right_probe_avg']
            final_offset = (right_avg - left_avg) + self.z_calibrate_compensation
            output = (
                f"Left nozzle probe average:  {left_avg:.6f}\n"
                f"Right nozzle probe average: {right_avg:.6f}\n"
                f"Final dual-nozzle offset:   {final_offset:.6f}\n"
            )
            if not (-self.max_z_offset <= final_offset <= self.max_z_offset):
                error_msg = (
                    f"Final offset {final_offset:.3f} is out of range "
                    f"[-{self.max_z_offset}, {self.max_z_offset}]. "
                    "Please check and recalibrate.\n"
                )
                output += error_msg

        except Exception as e:
            output = f"Dual-Nozzle Offset Calculation Error: {str(e)}\n"

        finally:
            self.gcmd.respond_info(output)
            self._reset_state()

    def _reset_state(self):
        self.state = CalibrationState.INIT
        self._waiting = False
        self.next_callback = None

    def _get_probe_accuracy_avg(self, probe_name):
        pch = self.printer.lookup_object(probe_name)
        status = pch.get_status(self.printer.get_reactor().monotonic())
        return float(status.get('accuracy_avg', 0.0))

# Main external probe interface
class PrinterDualProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.probe_name = config.get_name().split(' ')[-1]
        self.printer.add_object(self.probe_name,self)
        self.mcu_probe = probe.ProbeEndstopWrapper(config)
        self.cmd_helper = OffsetProbeCommandHelper(config, self,
                                             self.mcu_probe.query_endstop)
        self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.probe_session = probe.ProbeSessionHelper(config, self.mcu_probe)
        self.mcu_probe.probe_session = self.probe_session
        self.dual_probe_calibrate = DualProbeCalibrator(config)

    def get_probe_params(self, gcmd=None):
        return self.probe_session.get_probe_params(gcmd)
    def get_offsets(self):
        return self.probe_offsets.get_offsets()
    def get_status(self, eventtime):
        return self.cmd_helper.get_status(eventtime)
    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)

def load_config(config):
    return PrinterDualProbe(config)

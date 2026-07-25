# Mechanicaly conforms a moving gantry to the bed with 4 Z steppers
#
# Copyright (C) 2018  Maks Zolin <mzolin@vorondesign.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import operation_progress, probe, z_tilt

# Leveling code for XY rails that are controlled by Z steppers as in:
#
# Z stepper1 ----> O                             O <---- Z stepper2
#                  | * <-- probe1   probe2 --> * |
#                  |                             |
#                  |                             | <--- Y2 rail
#   Y1 rail -----> |                             |
#                  |                             |
#                  |=============================|
#                  |            ^                |
#                  |            |                |
#                  |   X rail --/                |
#                  |                             |
#                  | * <-- probe0   probe3 --> * |
# Z stepper0 ----> O                             O <---- Z stepper3

class QuadGantryLevel:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.retry_helper = z_tilt.RetryHelper(config,
            "Possibly Z motor numbering is wrong")
        self.max_adjust = config.getfloat("max_adjust", 4, above=0)
        self.horizontal_move_z = config.getfloat("horizontal_move_z", 5.0)
        self.probe_helper = probe.ProbePointsHelper(config, self.probe_finalize)
        self.probe_helper.set_probe_progress_callback(
            self._report_probe_progress)
        if len(self.probe_helper.probe_points) != 4:
            raise config.error(
                "Need exactly 4 probe points for quad_gantry_level")
        self.z_status = z_tilt.ZAdjustStatus(self.printer)
        self.z_helper = z_tilt.ZAdjustHelper(config, 4)
        self.gantry_corners = config.getlists('gantry_corners', parser=float,
                                              seps=(',', '\n'), count=2)
        if len(self.gantry_corners) < 2:
            raise config.error(
                "quad_gantry_level requires at least two gantry_corners")
        # Register QUAD_GANTRY_LEVEL command
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command(
            'QUAD_GANTRY_LEVEL', self.cmd_QUAD_GANTRY_LEVEL,
            desc=self.cmd_QUAD_GANTRY_LEVEL_help)
    cmd_QUAD_GANTRY_LEVEL_help = (
        "Conform a moving, twistable gantry to the shape of a stationary bed")
    def cmd_QUAD_GANTRY_LEVEL(self, gcmd):
        self.z_status.reset()
        self.retry_helper.start(gcmd)
        self.verify_after_adjust = bool(gcmd.get_int(
            'VERIFY', 0, minval=0, maxval=1))
        self.verification_pending = False
        self.operation_round = 1
        operation_progress.respond(
            self.gcode, 'begin', id='quad_gantry_level',
            type='quad_gantry_level', step=0, total=4, phase='homing',
            round=1)
        self.gcode.run_script_from_command('G28')
        operation_progress.respond(
            self.gcode, 'update', id='quad_gantry_level',
            type='quad_gantry_level', step=0, total=4, phase='probing',
            round=1)
        self.probe_helper.start_probe(gcmd, probe_method="probe")
    def _report_probe_progress(self, step, total, result):
        fields = {}
        if result is not None:
            fields = {
                'point': step,
                'probe_x': '%.3f' % (result[0],),
                'probe_y': '%.3f' % (result[1],),
                'probe_z': '%.6f' % (result[2],),
            }
        operation_progress.respond(
            self.gcode, 'update', id='quad_gantry_level',
            type='quad_gantry_level', step=step, total=total,
            phase='probing', round=self.operation_round, **fields)
    def _report_measurements(self, phase, round_number, **fields):
        operation_progress.respond(
            self.gcode, 'measurements', id='quad_gantry_level',
            type='quad_gantry_level', phase=phase, round=round_number,
            **fields)
    def _start_verification_round(self):
        self.verification_pending = True
        self.operation_round += 1
        operation_progress.respond(
            self.gcode, 'update', id='quad_gantry_level',
            type='quad_gantry_level', step=0, total=4, phase='verifying',
            round=self.operation_round)
    def probe_finalize(self, offsets, positions):
        round_number = self.operation_round
        # Mirror our perspective so the adjustments make sense
        # from the perspective of the gantry
        z_positions = [self.horizontal_move_z - p[2] for p in positions]
        probe_range = max(z_positions) - min(z_positions)
        self._report_measurements(
            'measured', round_number,
            probe0='%.6f' % (positions[0][2],),
            probe1='%.6f' % (positions[1][2],),
            probe2='%.6f' % (positions[2][2],),
            probe3='%.6f' % (positions[3][2],),
            range='%.6f' % (probe_range,),
            tolerance='%.6f' % (self.retry_helper.retry_tolerance,))
        if (self.verification_pending
                and probe_range <= self.retry_helper.retry_tolerance):
            operation_progress.respond(
                self.gcode, 'update', id='quad_gantry_level',
                type='quad_gantry_level', step=4, total=4,
                phase='verified', round=round_number)
            result = self.z_status.check_retry_result('done')
            operation_progress.respond(
                self.gcode, 'end', id='quad_gantry_level',
                type='quad_gantry_level', result='completed')
            return result
        self.verification_pending = False
        points_message = "Gantry-relative probe points:\n%s\n" % (
            " ".join(["%s: %.6f" % (z_id, z_positions[z_id])
                for z_id in range(len(z_positions))]))
        self.gcode.respond_info(points_message)
        # Calculate slope along X axis between probe point 0 and 3
        ppx0 = [positions[0][0] + offsets[0], z_positions[0]]
        ppx3 = [positions[3][0] + offsets[0], z_positions[3]]
        slope_x_pp03 = self.linefit(ppx0, ppx3)
        # Calculate slope along X axis between probe point 1 and 2
        ppx1 = [positions[1][0] + offsets[0], z_positions[1]]
        ppx2 = [positions[2][0] + offsets[0], z_positions[2]]
        slope_x_pp12 = self.linefit(ppx1, ppx2)
        logging.info("quad_gantry_level f1: %s, f2: %s"
                     % (slope_x_pp03, slope_x_pp12))
        # Calculate gantry slope along Y axis between stepper 0 and 1
        a1 = [positions[0][1] + offsets[1],
              self.plot(slope_x_pp03, self.gantry_corners[0][0])]
        a2 = [positions[1][1] + offsets[1],
              self.plot(slope_x_pp12, self.gantry_corners[0][0])]
        slope_y_s01 = self.linefit(a1, a2)
        # Calculate gantry slope along Y axis between stepper 2 and 3
        b1 = [positions[0][1] + offsets[1],
              self.plot(slope_x_pp03, self.gantry_corners[1][0])]
        b2 = [positions[1][1] + offsets[1],
              self.plot(slope_x_pp12, self.gantry_corners[1][0])]
        slope_y_s23 = self.linefit(b1, b2)
        logging.info("quad_gantry_level af: %s, bf: %s"
                     % (slope_y_s01, slope_y_s23))
        # Calculate z height of each stepper
        z_height = [0,0,0,0]
        z_height[0] = self.plot(slope_y_s01, self.gantry_corners[0][1])
        z_height[1] = self.plot(slope_y_s01, self.gantry_corners[1][1])
        z_height[2] = self.plot(slope_y_s23, self.gantry_corners[1][1])
        z_height[3] = self.plot(slope_y_s23, self.gantry_corners[0][1])

        ainfo = zip(["z","z1","z2","z3"], z_height[0:4])
        apos = " ".join(["%s: %06f" % (x) for x in ainfo])
        self.gcode.respond_info("Actuator Positions:\n" + apos)

        z_ave = sum(z_height) / len(z_height)
        self.gcode.respond_info("Average: %0.6f" % z_ave)
        z_adjust = []
        for z in z_height:
            z_adjust.append(z_ave - z)

        adjust_max = max(z_adjust)
        if adjust_max > self.max_adjust:
            operation_progress.respond(
                self.gcode, 'error', id='quad_gantry_level',
                type='quad_gantry_level', result='failed',
                reason='max_adjust_exceeded')
            raise self.gcode.error("Aborting quad_gantry_level"
                                   " required adjustment %0.6f"
                                   " is greater than max_adjust %0.6f"
                                   % (adjust_max, self.max_adjust))

        speed = self.probe_helper.get_lift_speed()
        operation_progress.respond(
            self.gcode, 'update', id='quad_gantry_level',
            type='quad_gantry_level', step=4, total=4, phase='adjusting',
            round=round_number)
        self.z_helper.adjust_steppers(z_adjust, speed)
        self._report_measurements(
            'adjusting', round_number,
            adjust0='%.6f' % (z_adjust[0],),
            adjust1='%.6f' % (z_adjust[1],),
            adjust2='%.6f' % (z_adjust[2],),
            adjust3='%.6f' % (z_adjust[3],))
        operation_progress.respond(
            self.gcode, 'update', id='quad_gantry_level',
            type='quad_gantry_level', step=4, total=4, phase='evaluating',
            round=round_number)
        retry_result = self.retry_helper.check_retry(z_positions)
        if retry_result == 'done':
            if self.verify_after_adjust:
                self._start_verification_round()
                return 'retry'
            result = self.z_status.check_retry_result(retry_result)
            operation_progress.respond(
                self.gcode, 'end', id='quad_gantry_level',
                type='quad_gantry_level', result='completed')
        else:
            result = self.z_status.check_retry_result(retry_result)
            self.operation_round += 1
            operation_progress.respond(
                self.gcode, 'update', id='quad_gantry_level',
                type='quad_gantry_level', step=0, total=4, phase='probing',
                round=self.operation_round)
        return result

    def linefit(self,p1,p2):
        if p1[1] == p2[1]:
            # Straight line
            return 0,p1[1]
        m = (p2[1] - p1[1])/(p2[0] - p1[0])
        b = p1[1] - m * p1[0]
        return m,b
    def plot(self,f,x):
        return f[0]*x + f[1]
    def get_status(self, eventtime):
        return self.z_status.get_status(eventtime)

def load_config(config):
    return QuadGantryLevel(config)

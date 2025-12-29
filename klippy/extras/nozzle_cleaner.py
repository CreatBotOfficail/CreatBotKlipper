# Nozzle cleaning plugin with configurable modes
#
# Copyright (C) 2023  Your Name <your.email@example.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

class NozzleCleaner:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self.name = config.get_name()
        self.clean_x_min = config.getfloat('clean_x_min')
        self.clean_x_max = config.getfloat('clean_x_max')
        self.clean_y_min = config.getfloat('clean_y_min')
        self.clean_y_max = config.getfloat('clean_y_max')

        self.run_x_min, self.run_x_max = self.clean_x_min, self.clean_x_max
        self.run_y_min, self.run_y_max = self.clean_y_min, self.clean_y_max

        self.clean_z_height = config.getfloat('clean_z_height', 2.0)
        self.retract_z_height = config.getfloat('retract_z_height', 20.0)
        self.move_speed = config.getfloat('move_speed', 6000.0)
        self.clean_speed = config.getfloat('clean_speed', 4000.0)
        self.hotend_temp = config.getfloat('hotend_temp', 150.0)
        self.nozzle_loops = config.getint('nozzle_loops', 2)
        self.nozzle_loop_x = config.getint('nozzle_loop_x', 20)
        self.nozzle_loop_y = config.getint('nozzle_loop_y', 30)

        self.x_step = (self.run_x_max - self.run_x_min) / self.nozzle_loop_x
        self.y_step = (self.run_y_max - self.run_y_min) / self.nozzle_loop_y
        self.printer.add_object('nozzle_cleaner', self)
        self.gcode.register_command('CLEAN_NOZZLE', self.cmd_CLEAN_NOZZLE,
                                    desc=self.cmd_CLEAN_NOZZLE_help)

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.extruders = []
        for i in range(99):
            section = 'extruder'
            if i > 0:
                section = f'extruder{i}'
            try:
                extruder = self.printer.lookup_object(section)
                self.extruders.append(extruder)
            except:
                break
        if not self.extruders:
            try:
                self.extruders.append(self.printer.lookup_object('extruder'))
            except:
                pass

    cmd_CLEAN_NOZZLE_help = "Clean the nozzle with configurable modes"
    def cmd_CLEAN_NOZZLE(self, gcmd):
        try:
            self.apply_offset()
            self._set_temperature(self.hotend_temp, wait=False)
            self._ensure_homed()
            self._set_temperature(self.hotend_temp)
            self.gcode.run_script_from_command(f'G90')
            for i, extruder in enumerate(self.extruders):
                self.gcode.run_script_from_command(f'T{i}')
                self._move_to_clean_position()
                self._turn_off_heaters(i)
                self._clean_nozzle()
                self._return_to_safe_position()
            self.gcode.run_script_from_command(f'T0')
            gcmd.respond_info(f"All nozzles cleaning completed")
        except Exception as e:
            logging.exception(f"Error during nozzle cleaning: {str(e)}")
            try:
                self._return_to_safe_position()
                self._turn_off_heaters()
            except:
                pass
            raise gcmd.error(f"Nozzle cleaning failed!")

    def apply_offset(self):
        save_variables = self.printer.lookup_object('save_variables').allVariables
        if 'camera_x_offset_val' in save_variables:
            self.run_x_min = self.clean_x_min + save_variables['camera_x_offset_val']
            self.run_x_max = self.clean_x_max + save_variables['camera_x_offset_val']

        if 'camera_y_offset_val' in save_variables:
            self.run_y_min = self.clean_y_min + save_variables['camera_y_offset_val']
            self.run_y_max = self.clean_y_max + save_variables['camera_y_offset_val']

    def _ensure_homed(self):
        curtime = self.printer.get_reactor().monotonic()
        kin_status = self.toolhead.get_kinematics().get_status(curtime)
        if ('x' not in kin_status['homed_axes'] or
            'y' not in kin_status['homed_axes'] or
            'z' not in kin_status['homed_axes']):
            self.gcode.run_script_from_command('G28')

    def _set_temperature(self, temp, wait=True):
        for i, _ in enumerate(self.extruders):
            self.gcode.run_script_from_command(f' M104 T{i} S{temp}')
        if wait:
            for i, _ in enumerate(self.extruders):
                self.gcode.run_script_from_command(f' M109 T{i} S{temp}')

    def _move_to_clean_position(self):
        script = f'G90\n'
        script += f'G0 Z{self.retract_z_height} F{self.move_speed}\n'
        script += f'G0 X{self.run_x_min} Y{self.run_y_max} F{self.move_speed}\n'
        script += f'G0 Z{self.clean_z_height} F{self.clean_speed}'
        self.gcode.run_script_from_command(script)
        self.toolhead.wait_moves()

    def _clean_nozzle(self):
        gcode_commands = []
        for i in range(self.nozzle_loops):
            current_y = self.run_y_max
            current_step = self.y_step
            for i in range(self.nozzle_loop_x):
                current_y += current_step
                if current_y < self.run_y_min or current_y > self.run_y_max:
                    current_step = -current_step
                    current_y += 2 * current_step
                if i % 2 == 0:
                    gcode_commands.append(f'G1 X{self.run_x_max} Y{current_y} F{self.clean_speed}')
                else:
                    gcode_commands.append(f'G1 X{self.run_x_min} Y{current_y} F{self.clean_speed}')
            gcode_commands.append(f'G0 X{self.run_x_min} Y{self.run_y_max} F{self.move_speed}')
            current_x = self.run_x_min
            for i in range(self.nozzle_loop_y):
                current_x += self.x_step
                if current_x > self.run_x_max:
                    break
                if i % 2 == 0:
                    gcode_commands.append(f'G1 X{current_x} Y{self.run_y_min} F{self.clean_speed}')
                else:
                    gcode_commands.append(f'G1 X{current_x} Y{self.run_y_max} F{self.clean_speed}')
        self.gcode.run_script_from_command('\n'.join(gcode_commands))
        self.toolhead.wait_moves()

    def _return_to_safe_position(self):
        self.gcode.run_script_from_command(f'G0 Z{self.retract_z_height} F{self.move_speed}')
        self.toolhead.wait_moves()

    def _turn_off_heaters(self, extruder_index=None):
        if extruder_index is not None:
            self.gcode.run_script_from_command(f'M104 T{extruder_index} S0')
        else:
            for i, _ in enumerate(self.extruders):
                self.gcode.run_script_from_command(f'M104 T{i} S0')

def load_config(config):
    return NozzleCleaner(config)

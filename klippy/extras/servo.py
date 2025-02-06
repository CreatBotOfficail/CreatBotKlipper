# Support for servos
#
# Copyright (C) 2017-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

SERVO_SIGNAL_PERIOD = 0.020
PIN_MIN_TIME = 0.100

class PrinterServo:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.min_width = config.getfloat('minimum_pulse_width', .001,
                                         above=0., below=SERVO_SIGNAL_PERIOD)
        self.max_width = config.getfloat('maximum_pulse_width', .002,
                                         above=self.min_width,
                                         below=SERVO_SIGNAL_PERIOD)
        self.max_angle = config.getfloat('maximum_servo_angle', 180.)
        self.signal_duration = config.getfloat('signal_duration', 0, minval=0.)
        self.steps_decomposed = config.getint('steps_decomposed', 0)
        self.angle_to_width = (self.max_width - self.min_width) / self.max_angle
        self.width_to_value = 1. / SERVO_SIGNAL_PERIOD
        self.last_value = self.last_value_time = 0.
        self.initial_pwm = 0.
        iangle = config.getfloat('initial_angle', None, minval=0., maxval=360.)
        if iangle is not None:
            self.initial_pwm = self._get_pwm_from_angle(iangle)
        else:
            iwidth = config.getfloat('initial_pulse_width', 0.,
                                     minval=0., maxval=self.max_width)
            self.initial_pwm = self._get_pwm_from_pulse_width(iwidth)
        # Setup mcu_servo pin
        ppins = self.printer.lookup_object('pins')
        self.mcu_servo = ppins.setup_pin('pwm', config.get('pin'))
        self.mcu_servo.setup_max_duration(0.)
        self.mcu_servo.setup_cycle_time(SERVO_SIGNAL_PERIOD)
        self.mcu_servo.setup_start_value(self.initial_pwm, 0.)
        # Register event handler
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        # Register commands
        servo_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_SERVO", "SERVO", servo_name,
                                   self.cmd_SET_SERVO,
                                   desc=self.cmd_SET_SERVO_help)
    def get_status(self, eventtime):
        return {'value': self.last_value}
    def _set_pwm(self, print_time, value):
        if value == self.last_value:
            return
        print_time = max(print_time, self.last_value_time + PIN_MIN_TIME)
        self.mcu_servo.set_pwm(print_time, value)
        self.last_value = value
        self.last_value_time = print_time
        self._handle_signal_duration(print_time)
    def _get_s_curve_value(self, last_value, value, t):
        smooth_factor = t * t * (3 - 2 * t)
        return last_value + smooth_factor * (value - last_value)
    def _set_low_pwm(self, print_time, value):
        if value == self.last_value:
            return
        if self.last_value == 0:
            self.last_value = self.initial_pwm
        steps = self.steps_decomposed
        for step in range(steps):
            t = step / (steps - 1)
            current_value = self._get_s_curve_value(self.last_value, value, t)
            print_time = max(print_time, self.last_value_time + 0.02)
            self.mcu_servo.set_pwm(print_time, current_value)
            self.last_value_time = print_time
        self.last_value = value
        self._handle_signal_duration(print_time)
    def _handle_signal_duration(self, print_time):
        if self.signal_duration:
            if abs(self.signal_duration / 0.02 - round(self.signal_duration / 0.02)) < 1e-9:
                self.signal_duration -= 0.001
            print_time = max(print_time + SERVO_SIGNAL_PERIOD, print_time + self.signal_duration)
            self.mcu_servo.set_pwm(print_time, 0)
            self.last_value_time = print_time
    def _handle_ready(self):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        self._set_pwm(print_time, self.initial_pwm)
    def _get_pwm_from_angle(self, angle):
        angle = max(0., min(self.max_angle, angle))
        width = self.min_width + angle * self.angle_to_width
        return width * self.width_to_value
    def _get_pwm_from_pulse_width(self, width):
        if width:
            width = max(self.min_width, min(self.max_width, width))
        return width * self.width_to_value
    cmd_SET_SERVO_help = "Set servo angle"
    def cmd_SET_SERVO(self, gcmd):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        print_time = max(print_time, self.last_value_time)
        width = gcmd.get_float('WIDTH', None)
        if width is not None:
            if self.steps_decomposed:
                self._set_low_pwm(print_time, self._get_pwm_from_pulse_width(width))
            else:
                self._set_pwm(print_time, self._get_pwm_from_pulse_width(width))
        else:
            angle = gcmd.get_float('ANGLE')
            if self.steps_decomposed:
                self._set_low_pwm(print_time, self._get_pwm_from_angle(angle))
            else:
                self._set_pwm(print_time, self._get_pwm_from_angle(angle))

def load_config_prefix(config):
    return PrinterServo(config)

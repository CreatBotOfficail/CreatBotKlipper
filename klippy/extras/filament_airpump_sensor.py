# Filament air pump load Module
#
# Copyright (C) 2025 Creatbot
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import output_pin, filament_switch_sensor

class AirPump:
    def __init__(self, config, timeout_event_handler):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.max_run_time = config.getfloat('max_run_time', 10., minval=0.)
        self.insert_delay_time = config.getfloat('insert_delay_time', 2., minval=0.)
        self.start_time = None
        self.is_running = False
        self.timeout_timer = None

        ppins = self.printer.lookup_object('pins')
        self.airpump_pin = None
        airpump_pin = config.get('airpump_pin')
        self.airpump_pin = ppins.setup_pin('digital_out', airpump_pin)
        self.airpump_pin.setup_max_duration(0.)
        self.airpump_mcu = self.airpump_pin.get_mcu()
        self.timeout_event_handler = timeout_event_handler

        self.motor_pin = None
        motor_pin = config.get('motor_pin')
        self.motor_pin = ppins.setup_pin('digital_out', motor_pin)
        self.motor_pin.setup_max_duration(0.)

    def set_power(self, power, eventtime):
        print_time = self.airpump_mcu.estimated_print_time(eventtime + 0.1)
        logging.info(f"set air pump to {power} at {print_time}")
        if power > 0:
            if not self.is_running:
                self.start_time = self.reactor.monotonic()
                self.is_running = True
                self.timeout_timer = self.reactor.register_timer(
                    self._check_timeout, self.start_time + self.max_run_time)
                self.motor_pin.set_digital(print_time, 1)
                self.airpump_pin.set_digital(print_time, 1)
        else:
            if self.timeout_timer is not None:
                self.reactor.unregister_timer(self.timeout_timer)
                self.timeout_timer = None
            self.is_running = False
            self.start_time = None
            self.motor_pin.set_digital(print_time, 0)
            self.airpump_pin.set_digital(print_time, 0)

    def _check_timeout(self, eventtime):
        if self.is_running and (eventtime - self.start_time >= self.max_run_time):
            if self.timeout_event_handler:
                self.timeout_event_handler(eventtime)
            logging.info(f"Air pump '{self.name}' timed out after {self.max_run_time}s")
        return self.reactor.NEVER

class AirPump_Helper(filament_switch_sensor.RunoutHelper):
    def __init__(self, config):
        super().__init__(config)
        self.airpump = AirPump(config, self._timeout_event_handler)
        self.retry_count = 0

    def set_airpump_state(self, power, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        self.airpump.set_power(power, eventtime)

    def _runout_event_handler(self, eventtime):
        self.set_airpump_state(1, eventtime)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def _insert_pump_event_handler(self, eventtime):
        delay = self.airpump.insert_delay_time
        target_time = eventtime + delay
        def delayed_action(eventtime):
            self.set_airpump_state(0, eventtime)
            self.min_event_systime = self.reactor.monotonic() + self.event_delay
            return self.reactor.NEVER
        self.reactor.register_callback(delayed_action, target_time)
        self.retry_count = 0

    def _timeout_event_handler(self, eventtime):
        self.retry_count += 1
        self.set_airpump_state(0, eventtime)
        if self.retry_count < 3:
            target_time = eventtime + 30
            def retry_action(eventtime):
                self._runout_event_handler(eventtime)
                return self.reactor.NEVER
            self.reactor.register_callback(retry_action, target_time)
        else:
            super()._runout_event_handler(eventtime)
            self.retry_count = 0

class AirCleanController:
    def __init__(self, config, printer):
        self.printer = printer
        self.reactor = printer.get_reactor()

        ppins = printer.lookup_object('pins')
        air_clean_pin = config.get('air_clean_pin')
        self.air_clean_pin = ppins.setup_pin('digital_out', air_clean_pin)
        self.air_clean_pin.setup_max_duration(0.)
        self.air_clean_pin.setup_start_value(0., 0.)
        self.airclean_mcu = self.air_clean_pin.get_mcu()

        self.run_interval = config.getfloat('run_interval', 300., minval=60.)
        self.run_time = config.getfloat('run_time', 2., minval=1.)
        self.is_ready = False
        self.timer = None
        self.airpump_active = False
        self.next_activation_time = 0.0
        printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.is_ready = True
        self.timer = self.reactor.register_timer(self._update_callback, self.reactor.NOW)

    def _update_callback(self, eventtime):
        if not self.is_ready:
            return eventtime + 1.0
        print_stats = self.printer.lookup_object('print_stats')
        status = print_stats.get_status(eventtime)
        current_state = status.get('state', 'standby')
        if current_state == 'printing':
            if eventtime >= self.next_activation_time and not self.airpump_active:
                self._set_airpump_state(eventtime, True)
        else:
            self.next_activation_time = eventtime + self.run_interval
            if self.airpump_active:
                self._set_airpump_state(eventtime, False)

        return eventtime + 0.5

    def _set_airpump_state(self, eventtime, state):
        print_time = self.airclean_mcu.estimated_print_time(eventtime + 0.1)
        if state:
            if not self.airpump_active:
                self.air_clean_pin.set_digital(print_time, 1.0)
                self.airpump_active = True
                logging.info("Air pump activated at printing state")
                self.reactor.register_timer(
                    self._auto_stop_pump,
                    eventtime + self.run_time
                )
        else:
            if self.airpump_active:
                self.air_clean_pin.set_digital(print_time, 0.0)
                self.airpump_active = False
                logging.info("Air pump deactivated")

    def _auto_stop_pump(self, eventtime):
        self._set_airpump_state(eventtime, False)
        self.next_activation_time = eventtime + self.run_interval
        return self.reactor.NEVER

class AirPumpLoad:
    def __init__(self, config):
        self.cmd_SET_AIRPUMP_help = "Sets the state of the filament airpump"
        self.printer = config.get_printer()
        buttons = self.printer.load_object(config, 'buttons')
        switch_pin = config.get('switch_pin')

        buttons.register_debounce_button(switch_pin, self._pump_status_handler, config)
        self.airpump_helper = AirPump_Helper(config)
        self.get_status = self.airpump_helper.get_status

        self.name = config.get_name().split()[-1]
        self.template_eval = output_pin.lookup_template_eval(config)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_AIRPUMP", "AIRPUMP",
                                   self.name,
                                   self.cmd_SET_AIRPUMP,
                                   desc=self.cmd_SET_AIRPUMP_help)
        self.air_clean_controller = AirCleanController(config, self.printer)

    def _pump_status_handler(self, eventtime, state):
        self.airpump_helper.note_filament_present(eventtime, state)

    def _template_update(self, text):
        try:
            value = float(text)
        except ValueError as e:
            logging.exception("Airpump template render error")
            value = 0.
        self.airpump_helper.set_airpump_state(value)

    def cmd_SET_AIRPUMP(self, gcmd):
        value = gcmd.get_float('VALUE', None, minval=0., maxval=1.)
        template = gcmd.get('TEMPLATE', None)
        run_time = gcmd.get_float('RUN_TIME', None, minval=0.)

        if (value is None) == (template is None):
            raise gcmd.error("SET_AIRPUMP must specify VALUE or TEMPLATE")
        if template is not None:
            self.template_eval.set_template(gcmd, self._template_update)
            return
        self.airpump_helper.set_airpump_state(value)
        gcmd.respond_info(f"{self.name} airpump set to: {value}")

        if value > 0 and run_time is not None:
            eventtime = self.airpump_helper.reactor.monotonic()
            def turn_off_pump(eventtime):
                self.airpump_helper.set_airpump_state(0, eventtime)
                return self.airpump_helper.reactor.NEVER
            self.airpump_helper.reactor.register_timer(turn_off_pump, eventtime + run_time)

def load_config_prefix(config):
    return AirPumpLoad(config)

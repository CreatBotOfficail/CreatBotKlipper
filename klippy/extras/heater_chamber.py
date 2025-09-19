# Support for a heated chamber with staged preheating
#
# Copyright (C) 2025 CreatBot
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import threading

MAX_HEAT_TIME = 10.0


class PrinterHeaterChamber:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.heater_pins = self._collect_heater_pins(config)

        self.staged_preheat_enable = (len(self.heater_pins) > 1 and 
                                    config.getboolean('staged_preheat_enable', True))
        self.preheat_duration = config.getfloat('preheat_duration', 5.0, above=0.)
        self.pwm_cycle_time = config.getfloat('pwm_cycle_time', 0.300, above=0.)
        self.cold_start_threshold = config.getfloat('cold_start_threshold', 3.0, above=0.)

        pheaters = self.printer.load_object(config, 'heaters')
        self.heater = pheaters.setup_heater(config, 'C')
        self.get_status = self.heater.get_status
        self.stats = self.heater.stats
        self.heater.set_pwm = self.set_pwm
        self.heaters = self._setup_heater_pins()

        self._init_preheat_variables()
        
        self.lock = threading.Lock()
        self.reactor = self.printer.get_reactor()
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

    def _init_preheat_variables(self):
        if self.staged_preheat_enable:
            self.needs_preheat = True
            self.is_preheating = False
            self.preheat_timer = None
            self._current_preheat_heater = 0
        else:
            self.needs_preheat = False
            self.is_preheating = False
            self.preheat_timer = None
            self._current_preheat_heater = 0
        
        self.heating_time = 0

    def _collect_heater_pins(self, config):
        heater_pins = []
        
        # Get main heater pin
        heater_pin = config.get('heater_pin', None)
        if heater_pin:
            heater_pins.append(heater_pin)
        
        # Get additional heater pins (heater_pin1, heater_pin2, etc.)
        pin_index = 1
        while True:
            pin_name = f'heater_pin{pin_index}'
            pin = config.get(pin_name, None)
            if pin is None:
                break
            heater_pins.append(pin)
            pin_index += 1

        if not heater_pins:
            raise config.error("Must specify at least one heater pin (heater_pin, heater_pin1, etc.)")

        if not config.get('heater_pin', None):
            raise config.error("heater_pin must be explicitly configured")

        logging.info(f"Found {len(heater_pins)} heater pins: {heater_pins}")
        return heater_pins

    def _setup_heater_pins(self):
        heaters = []
        ppins = self.printer.lookup_object('pins')

        for i, pin in enumerate(self.heater_pins):
            try:
                if i == 0:
                    # Use parent heater's PWM for first heater
                    mcu_pwm = self.heater.mcu_pwm
                else:
                    # Create new PWM for additional heaters
                    mcu_pwm = ppins.setup_pin('pwm', pin)
                    mcu_pwm.setup_cycle_time(self.pwm_cycle_time)
                    mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
                
                heaters.append({
                    'pin': pin,
                    'mcu_pwm': mcu_pwm,
                    'index': i
                })
                logging.info(f"Heater {i+1} configured on pin {pin}")
                
            except Exception as e:
                logging.error(f"Failed to setup heater {i+1} on pin {pin}: {e}")

        if not heaters:
            raise config.error("No heaters could be configured successfully")

        logging.info(f"Successfully initialized {len(heaters)} heaters")
        return heaters

    def set_pwm(self, read_time, value):
        if self.heater.target_temp <= 0. or read_time > self.heater.verify_mainthread_time:
            value = 0.

        if self.staged_preheat_enable:
            is_idle = not self.needs_preheat and not self.is_preheating
            idle_for_too_long = (read_time - self.heating_time) > self.cold_start_threshold
            if is_idle and idle_for_too_long:
                self.needs_preheat = True

        if ((read_time < self.heater.next_pwm_time or not self.heater.last_pwm_value)
            and abs(value - self.heater.last_pwm_value) < 0.05):
            return

        pwm_time = read_time + self.heater.pwm_delay
        self.heater.next_pwm_time = pwm_time + 0.03 * MAX_HEAT_TIME
        self.heater.last_pwm_value = value
        self.heating_time = pwm_time

        self._control_heating(pwm_time, value)

    def _control_heating(self, pwm_time, value):
        if self.staged_preheat_enable and self.needs_preheat:
            self._start_preheat_mode()

        if self.is_preheating:
            self._activate_single_heater(pwm_time, value)
        else:
            self._activate_all_heaters(pwm_time, value)

    def _start_preheat_mode(self):
        if not self.staged_preheat_enable:
            return
        self.is_preheating = True
        self.needs_preheat = False
        self._start_preheat_timer()

    def _start_preheat_timer(self):
        self._stop_preheat_timer()
        current_time = self.reactor.monotonic()
        self.preheat_timer = self.reactor.register_timer(
            self._preheat_timer_callback, current_time + self.preheat_duration)

    def _stop_preheat_timer(self):
        if self.preheat_timer is not None:
            self.reactor.unregister_timer(self.preheat_timer)
            self.preheat_timer = None

    def _preheat_timer_callback(self, eventtime):
        self.is_preheating = False
        return self.reactor.NEVER

    def _activate_single_heater(self, pwm_time, value):
        total_heaters = len(self.heaters)
        if total_heaters == 0:
            return

        self._current_preheat_heater = (self._current_preheat_heater + 1) % total_heaters

        pwm_on = 1.0 if value else 0.0

        for i, heater in enumerate(self.heaters):
            pwm_value = pwm_on if i == self._current_preheat_heater else 0.0
            try:
                heater['mcu_pwm'].set_pwm(pwm_time, pwm_value)
            except Exception as e:
                logging.error(f"Failed to set PWM for heater {i+1}: {e}")

    def _activate_all_heaters(self, pwm_time, value):
        for i, heater in enumerate(self.heaters):
            try:
                heater['mcu_pwm'].set_pwm(pwm_time, value)
            except Exception as e:
                logging.error(f"Failed to set PWM for heater {i+1}: {e}")

    def _turn_off_all_heaters(self):
        pwm_time = self.reactor.monotonic()

        for i, heater in enumerate(self.heaters):
            try:
                heater['mcu_pwm'].set_pwm(pwm_time, 0.)
            except Exception as e:
                logging.error(f"Failed to turn off heater {i+1}: {e}")

        if self.staged_preheat_enable:
            self.needs_preheat = True
        self.is_preheating = False

    def _handle_shutdown(self):
        with self.lock:
            if self.staged_preheat_enable:
                self._stop_preheat_timer()
            self._turn_off_all_heaters()


def load_config(config):
    return PrinterHeaterChamber(config)
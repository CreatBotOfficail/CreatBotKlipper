import logging


class PrinterHeaterFilamentChamber:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.pheaters = self.printer.load_object(config, 'heaters')
        self.heater = self.pheaters.setup_heater(config, 'FC')
        self.stats = self.heater.stats

        self.reactor = self.printer.get_reactor()

        self.turnoff_timer = None
        self.set_duration = 0.
        self.timer_start_time = 0.

        self.original_set_temp = self.heater.set_temp

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        try:
            gcode = self.printer.lookup_object('gcode')
            mux_commands = gcode.mux_commands.get('SET_HEATER_TEMPERATURE')
            if mux_commands and self.heater.short_name in mux_commands[1]:
                mux_commands[1][self.heater.short_name] = self.set_temperature_extended
                logging.info(
                    "Enabled TIME parameter support for filament chamber heater")
            else:
                logging.warning("No MUX entry for heater: %s",
                                self.heater.short_name)
        except Exception as e:
            logging.error(
                "Failed to set up TIME support for filament chamber heater: %s", e)

    def get_status(self, eventtime):
        status = self.heater.get_status(eventtime)

        if self.turnoff_timer is not None:
            remaining_time = max(
                0., self.set_duration - (self.reactor.monotonic() - self.timer_start_time))
            status['auto_turnoff'] = {
                'set_duration': int(self.set_duration),
                'remaining_time':  int(round(remaining_time, 1))
            }
        else:
            status['auto_turnoff'] = None

        return status

    def set_temperature_extended(self, gcmd):
        time_delay = gcmd.get_float('TIME', None)
        target_temp = gcmd.get_float('TARGET', 0.)

        self._stop_turnoff_timer()

        self.original_set_temp(target_temp)

        if target_temp > 0:
            delay = self._get_timer_delay(time_delay)
            if delay > 0:
                self._start_turnoff_timer(delay)

    def _get_timer_delay(self, time_param):
        return time_param if time_param is not None and time_param > 0 else 0

    def _start_turnoff_timer(self, delay):
        current_time = self.reactor.monotonic()
        self.turnoff_timer = self.reactor.register_timer(
            self._turnoff_callback, current_time + delay)
        self.set_duration = delay
        self.timer_start_time = current_time
        logging.info(
            "Filament chamber heater auto-turnoff set for %.1f seconds", delay)

    def _stop_turnoff_timer(self):
        if self.turnoff_timer is not None:
            try:
                self.reactor.unregister_timer(self.turnoff_timer)
            except Exception as e:
                logging.warning("Failed to stop timer: %s", e)
            finally:
                self._clear_timer_state()

    def _clear_timer_state(self):
        self.turnoff_timer = None
        self.set_duration = 0.
        self.timer_start_time = 0.

    def _turnoff_callback(self, eventtime):
        logging.info("Auto-turning off filament chamber heater")
        self.original_set_temp(0.)
        self._clear_timer_state()
        return self.reactor.NEVER


def load_config(config):
    return PrinterHeaterFilamentChamber(config)

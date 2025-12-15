from . import led
import logging

class LEDStateHandler:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.led_name = config.get_name().split()[-1]
        self.color_map = {}
        states = ['idle', 'printing', 'paused', 'error', 'heating']
        for state in states:
            color_str = config.get(state, None)
            if color_str:
                r, g, b = [float(v.strip()) for v in color_str.split(',')]
                self.color_map[state] = (r, g, b)

        default_colors = {
            'idle': (0.3, 0.3, 0.3),
            'printing': (0.0, 0.0, 0.3),
            'paused': (0.3, 0.0, 0.0),
            'error': (0.3, 0.0, 0.0),
            'heating': (0.0, 0.0, 0.3),
        }
        for state, color in default_colors.items():
            if state not in self.color_map:
                self.color_map[state] = color

        self.led = led.PrinterPWMLED(config)
        self.hot_targets = {}
        self.is_ready = False
        self.last_color = None

        self.reactor = self.printer.get_reactor()
        self.timer = None

        self.print_stats = None
        self.heaters = None
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        self.print_stats = self.printer.lookup_object('print_stats')
        self.heaters = self.printer.lookup_object('heaters')

        for heater_name in self.heaters.get_all_heaters():
            self.hot_targets[heater_name] = 0.0

        self.is_ready = True
        self.timer = self.reactor.register_timer(
            self._update_callback, self.reactor.NOW)

    def _update_callback(self, eventtime):
        if not self.is_ready:
            return eventtime + 1.0

        status = self.print_stats.get_status(eventtime)
        current_state = status.get('state', 'standby')
        message = status.get('message', '')
        try:
            hot_targets_copy = self.hot_targets.copy()
            for heater_name in hot_targets_copy.keys():
                try:
                    if heater_name.startswith('heater_generic '):
                        heater_name = heater_name.split(' ')[-1]
                    heater = self.heaters.lookup_heater(heater_name)
                    _, target = heater.get_temp(eventtime)
                    self.hot_targets[heater_name] = target
                except Exception:
                    pass
        except Exception as e:
            logging.error("Error updating heater targets: %s", str(e))
            return eventtime + 1.0

        max_hot_target = max(self.hot_targets.values()) if self.hot_targets else 0

        if current_state == 'standby':
            color_name = 'heating' if max_hot_target > 0 else 'idle'
        elif current_state == 'printing':
            color_name = 'printing'
        elif current_state == 'paused':
            color_name = 'paused'
        elif current_state == 'cancelled':
            if message:
                color_name = 'paused'
            elif max_hot_target > 0:
                color_name = 'heating'
            else:
                color_name = 'idle'
        elif current_state == 'error':
            color_name = 'error'
        else:
            color_name = 'idle'

        try:
            r, g, b = self.color_map[color_name]
        except KeyError:
            logging.error("Missing color definition for state: %s", color_name)
            r, g, b = (1.0, 0.0, 0.0)

        if self.last_color != (r, g, b):
            try:
                color = (r, g, b, 0.0)
                self.led.led_helper._set_color(None, color)
                self.led.led_helper._check_transmit(print_time=None)
                logging.info('LED %s: %s %s', self.led_name, current_state, (r, g, b))
                self.last_color = (r, g, b)
            except Exception as e:
                logging.error("Error updating LED color: %s", str(e))

        return eventtime + 0.5

def load_config_prefix(config):
    return LEDStateHandler(config)
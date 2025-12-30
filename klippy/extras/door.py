# Door Detection with Lock Module
#
# Copyright (C) 2025 Creatbot
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

class Door:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        ppins = self.printer.lookup_object('pins')
        self.lock_pin = None
        self.lock_mcu = None
        lock_pin = config.get('lock_pin', None)
        self.delay_time = config.getfloat('delay_time', 1.0)
        if lock_pin is not None:
            self.lock_pin = ppins.setup_pin('digital_out', lock_pin)
            self.lock_pin.setup_max_duration(0.)
            self.lock_mcu = self.lock_pin.get_mcu()
        door_pin = config.get('pin')
        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_debounce_button(door_pin, self._door_status_handler, config)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self.locked = False

        if not hasattr(self.printer, '_door_commands_registered'):
            gcode = self.printer.lookup_object('gcode')
            gcode.register_command("SET_DOOR_FUNCTION", self.cmd_SET_DOOR_FUNCTION)
            gcode.register_command("SET_DOOR_LOCK", self.cmd_SET_DOOR_LOCK)
            self.printer._door_commands_registered = True

        if not hasattr(self.printer, '_door_instances'):
            self.printer._door_instances = {}
        self.printer._door_instances[self.name] = self

        if not hasattr(self.printer, '_door_webhook_registered'):
            wh = self.printer.lookup_object('webhooks')
            wh.register_endpoint("door/set_lock", self._handle_set_lock_webhook)
            self.printer._door_webhook_registered = True

        if not hasattr(self.printer, '_door_start_print_wrapped'):
            self.printer._door_start_print_wrapped = True
        logging.info(f"Door '{self.name}' initialized")

    def handle_ready(self):
        self.save_variables = self.printer.lookup_object('save_variables')

    def _door_status_handler(self, eventtime, state):
        if state:
            self._set_lock_state(True, eventtime + self.delay_time)
        else:
            self._set_lock_state(False, eventtime)
            self._handle_door_open(eventtime)

    def _set_lock_state(self, locked, eventtime):
        self.locked = locked
        if self.lock_pin is None or self.lock_mcu is None:
            return
        print_time = self.lock_mcu.estimated_print_time(eventtime + 0.1)
        value = 1 if locked else 0
        self.lock_pin.set_digital(print_time, value)
        logging.info(f"Door '{self.name}' {'locked' if locked else 'unlocked'}")

    def _handle_door_open(self, eventtime):
        door_function = "Disabled"
        try:
            door_function = self.save_variables.allVariables.get("door_detect", "Disabled")
        except Exception as e:
            logging.error(f"Error getting door function: {e}")

        print_stats = self.printer.lookup_object('print_stats')
        status = print_stats.get_status(eventtime)
        current_state = status.get('state', 'standby')

        if current_state == 'printing' and door_function != "Disabled":
            gcode = self.printer.lookup_object('gcode')
            if door_function == 'Pause Print':
                logging.info(f"Door '{self.name}' opened during printing, issuing PAUSE")
                gcode.run_script_from_command("PAUSE")

    def cmd_SET_DOOR_FUNCTION(self, gcmd):
        raw_params = gcmd.get_command_parameters()
        function = raw_params.get("FUNCTION", "Disabled").strip()

        normalized_function = function.lower()
        valid_functions = {
            "disabled": "Disabled",
            "pause": "Pause Print",
            "pause print": "Pause Print"
        }

        if normalized_function not in valid_functions:
            raise gcmd.error(f"Invalid door function. Valid options: Disabled, Pause Print")
        standardized_function = valid_functions[normalized_function]
        if isinstance(standardized_function, str):
            value = f"'{standardized_function}'"
        else:
            value = str(standardized_function)

        script = f'SAVE_VARIABLE VARIABLE=door_detect VALUE="{value}"'
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command(script)
        gcmd.respond_info(f"Door function set to: {standardized_function}")

    def set_door_lock(self, door_name, state):
        if state not in ["lock", "unlock"]:
            return f"Error: STATE must be either 'lock' or 'unlock'"
        locked = state == "lock"
        eventtime = self.printer.get_reactor().monotonic()
        door_instances = getattr(self.printer, '_door_instances', {})

        if door_name == "all":
            if not door_instances:
                return "No door instances found" 
            for name, door in door_instances.items():
                door._set_lock_state(locked, eventtime)
            return f"All doors {'locked' if locked else 'unlocked'}"

        if door_name not in door_instances:
            return f"Error: Door '{door_name}' not found. Available doors: {', '.join(door_instances.keys())}"

        door = door_instances[door_name]
        door._set_lock_state(locked, eventtime)
        return f"Door '{door_name}' {'locked' if locked else 'unlocked'}"

    def cmd_SET_DOOR_LOCK(self, gcmd):
        door_name = gcmd.get("DOOR", "all").lower()
        state = gcmd.get("STATE", "").lower()
        response = self.set_door_lock(door_name, state)
        if response.startswith("Error:"):
            raise gcmd.error(response[7:])
        gcmd.respond_info(response)

    def _handle_set_lock_webhook(self, web_request):
        door_name = web_request.get_str("door", "all").lower()
        state = web_request.get_str("state", "").lower()
        response = self.set_door_lock(door_name, state)
        web_request.send({"result": response})

    def cmd_START_PRINT(self, gcmd):
        door_function = "Disabled"
        try:
            door_function = self.save_variables.allVariables.get("door_detect", "Disabled")
        except Exception as e:
            logging.error(f"Error getting door function: {e}")

        if door_function == "Disabled":
            if hasattr(self, 'prev_START_PRINT') and self.prev_START_PRINT:
                self.prev_START_PRINT(gcmd)
            return
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info("action:prompt_begin")
        gcode.respond_info("action:prompt_text Printer door is opened. Please close the door and then start printing.")
        gcode.respond_info("action:prompt_footer_button Ok|RESPOND TYPE=command MSG=action:prompt_end")
        gcode.respond_info("action:prompt_show")

    def get_status(self, eventtime=None):
        door_function = "Disabled"
        try:
            door_function = self.save_variables.allVariables.get("door_detect", "Disabled")
        except Exception as e:
            logging.error(f"Error getting door function: {e}")
        status = {
            'door_function': door_function,
            'doors': {}
        }

        door_instances = getattr(self.printer, '_door_instances', {})
        for door_name, door in door_instances.items():
            status['doors'][door_name] = {
                'locked': door.locked,
                'has_lock': door.lock_pin is not None
            }
        return status

def load_config_prefix(config):
    return Door(config)
# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, logging, io

VALID_GCODE_EXTS = ['gcode', 'g', 'gco']

DEFAULT_ERROR_GCODE = """
{% if 'heaters' in printer %}
   TURN_OFF_HEATERS
{% endif %}
"""

class VirtualSD:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:shutdown",
                                            self.handle_shutdown)
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        # File caching - prevent USB drive disconnection issues
        self.cache_enabled = config.getboolean('cache_enabled', False)
        self.cache_path = config.get('cache_path', '~/printer_data/cache')
        self.cache_path = os.path.normpath(os.path.expanduser(self.cache_path))
        self.cached_file = None
        self.original_file_path = None
        # Background copy related
        self.copy_thread = None
        self.copy_complete = False
        # Print Stat Tracking
        self.print_stats = self.printer.load_object(config, 'print_stats')
        # Work timer
        self.reactor = self.printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.next_file_position = 0
        self.file_line = 0
        self.file_runline = 0
        self.linfo = ""
        self.work_timer = None
        self.toolhead_pos = None
        # Error handling
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.on_error_gcode = gcode_macro.load_template(
            config, 'on_error_gcode', DEFAULT_ERROR_GCODE)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "SDCARD_RESET_FILE", self.cmd_SDCARD_RESET_FILE,
            desc=self.cmd_SDCARD_RESET_FILE_help)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc=self.cmd_SDCARD_PRINT_FILE_help)
        self.gcode.register_command(
            'GET_TASKLINE', self.cmd_GET_TASKLINE, False)
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self, check_subdirs=False):
        if check_subdirs:
            flist = []
            for root, dirs, files in os.walk(
                    self.sdcard_dirname, followlinks=True):
                for name in files:
                    ext = name[name.rfind('.')+1:]
                    if ext not in VALID_GCODE_EXTS:
                        continue
                    full_path = os.path.join(root, name)
                    r_path = full_path[len(self.sdcard_dirname) + 1:]
                    size = os.path.getsize(full_path)
                    flist.append((r_path, size))
            return sorted(flist, key=lambda f: f[0].lower())
        else:
            dname = self.sdcard_dirname
            try:
                filenames = os.listdir(self.sdcard_dirname)
                return [(fname, os.path.getsize(os.path.join(dname, fname)))
                        for fname in sorted(filenames, key=str.lower)
                        if not fname.startswith('.')
                        and os.path.isfile((os.path.join(dname, fname)))]
            except:
                logging.exception("virtual_sdcard get_file_list")
                raise self.gcode.error("Unable to get file list")
    def get_status(self, eventtime):
        return {
            'file_path': self.file_path(),
            'progress': self.progress(),
            'is_active': self.is_active(),
            'file_position': self.file_position,
            'file_line': self.file_runline,
            'file_size': self.file_size,
        }
    def file_path(self):
        if self.original_file_path:
            return self.original_file_path
        elif self.current_file:
            return self.current_file.name
        return None
    def progress(self):
        if self.file_size:
            return float(self.file_position) / self.file_size
        else:
            return 0.
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            self._get_runline()
            self._set_quick_pause()
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
            self.file_position = self._get_position_by_line(self.file_runline)
            self.file_line = self.file_runline
    def do_resume(self):
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self._cleanup_cache(clean_files=False)
        self.file_position = self.file_size = 0
        self.file_line = self.file_runline = 0
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.original_file_path = None
        self.file_position = self.file_size = 0
        self.file_line = self.file_runline = 0
        self.print_stats.reset()
        self.printer.send_event("virtual_sdcard:reset_file")
    
    def _cleanup_cache(self, clean_files=True):
        if self.copy_thread and self.copy_thread.is_alive():
            try:
                self.copy_thread.join(timeout=1.0)
            except:
                pass

        self.copy_thread = None
        self.copy_complete = False

        if clean_files and os.path.exists(self.cache_path):
            try:
                import glob
                cache_files = glob.glob(os.path.join(self.cache_path, "*"))
                for cache_file in cache_files:
                    os.remove(cache_file)
            except:
                pass

        self.cached_file = None
    
    def _is_removable_media(self, file_path):
        """Detect if file is on removable storage device"""
        try:
            import re
            import subprocess

            # Find device mount point
            result = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', '--target', file_path],
                                   capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return False

            device_path = result.stdout.strip()
            if not device_path:
                return False

            # Extract block device name (e.g., /dev/sdb1 -> sdb)
            match = re.search(r'/dev/([a-z]+)', device_path)
            if not match:
                return False

            block_device = match.group(1)

            removable_path = f'/sys/block/{block_device}/removable'
            if os.path.exists(removable_path):
                with open(removable_path, 'r') as f:
                    return f.read().strip() == '1'

        except:
            pass

        return False

    def async_copy_file(self, source_file, cache_file, original_file):
        try:
            with open(source_file, 'rb') as src, open(cache_file, 'wb') as dst:
                chunk_size = 64 * 1024  # 64KB chunks
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    dst.flush()

            self.copy_complete = True

            if original_file:
                try:
                    original_file.close()
                except:
                    pass

        except:
            self.copy_complete = False

    def _wait_for_cache_file(self, cache_file_path):
        import time
        time.sleep(0.1)
        for _ in range(100):  # Wait up to 10 seconds
            if os.path.exists(cache_file_path):
                try:
                    return io.open(cache_file_path, 'r', newline='')
                except:
                    pass
            time.sleep(0.1)
        return None

    def _handle_file_caching(self, gcmd, fname, f):
        is_removable = self._is_removable_media(fname)
        should_cache = self.cache_enabled and is_removable

        if should_cache:
            
            import threading
            self._cleanup_cache(clean_files=True)  # Only clean files when starting new cache
            try:
                if not os.path.exists(self.cache_path):
                    os.makedirs(self.cache_path)

                cache_file = os.path.join(self.cache_path, os.path.basename(fname))
                self.cached_file = cache_file

                self.copy_thread = threading.Thread(
                    target=self.async_copy_file,
                    args=(fname, cache_file, f),
                    daemon=True
                )
                self.copy_thread.start()

                cache_file_obj = self._wait_for_cache_file(cache_file)
                if cache_file_obj:
                    self.current_file = cache_file_obj
                else:
                    self.cached_file = None
                    gcmd.respond_raw("Cache file creation failed")

            except:
                self.cached_file = None
                gcmd.respond_raw("Cache failed, ensure USB connection is stable")

    cmd_SDCARD_RESET_FILE_help = "Clears a loaded SD File. Stops the print "\
        "if necessary"
    def _get_runline(self):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise self.gcode.error("Printer not ready")
        kin = toolhead.get_kinematics()
        steppers = kin.get_steppers()
        linfo = [(s.get_name(), s.get_stepper_taskline()) for s in steppers if s.get_name() != "stepper_z"]
        positions = [position for _, position in linfo if position != 0]
        max_position = min(positions) if positions else 0
        self.linfo = linfo
        self.file_runline = max_position

    def _set_quick_pause(self):
        toolhead = self.printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        toolhead.flush_step_generation()
        steppers = kin.get_steppers()

        def get_extruders():
            extruders = []
            for i in range(99):
                extruder_name = "extruder" if i == 0 else f"extruder{i}"
                extruder = self.printer.lookup_object(extruder_name, None)
                if extruder is None:
                    break
                extruders.append(extruder)
            return extruders
        for extruder in get_extruders():
            extruder.extruder_stepper.stepper.set_stepper_stop()
        for stepper in steppers:
            stepper.set_stepper_stop()
        for extruder in get_extruders():
            extruder.extruder_stepper.stepper.set_stepper_pause()
        kin_spos = {
            stepper.get_name(): stepper.mcu_to_commanded_position(stepper.set_stepper_pause())
            for stepper in steppers
        }
        self.toolhead_pos = kin.calc_position(kin_spos)
        self._set_kinematic_position(self.toolhead_pos)

    def _get_position_by_line(self, target_line):
        if target_line <= 0:
            return 0
        self.current_file.seek(0)
        current_line = 0
        position = 0
        while True:
            line = self.current_file.readline()
            if not line:
                raise ValueError(f"Target line number {target_line} exceeds the total number of lines in the file")
            if current_line == target_line - 1:
                logging.info(f"Starting position of target line {target_line}: {position}")
                return position
            if sys.version_info.major >= 3:
                line_bytes = len(line.encode('utf-8'))
            else:
                line_bytes = len(line)
            position += line_bytes
            current_line += 1

    def _set_kinematic_position(self, pos):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.get_last_move_time()
        curpos = toolhead.get_position()
        logging.info("set kinematic position pos=%.3f,%.3f,%.3f,%.3f",
                     pos[0], pos[1], pos[2], curpos[3])
        toolhead.set_position([pos[0], pos[1], pos[2], curpos[3]], homing_axes='xyz')

    def cmd_GET_TASKLINE(self, gcmd):
        if not self.must_pause_work:
            self._get_runline()
        formatted_pairs = [f"{key}:{value}" for key, value in self.linfo]
        formatted_line = " ".join(formatted_pairs)
        gcmd.respond_info(f"stepper line: {formatted_line}")
    def cmd_SDCARD_RESET_FILE(self, gcmd):
        if self.cmd_from_sd:
            raise gcmd.error(
                "SDCARD_RESET_FILE cannot be run from the sdcard")
        self._reset_file()
    cmd_SDCARD_PRINT_FILE_help = "Loads a SD file and starts the print.  May "\
        "include files in subdirectories."
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get("FILENAME")
        if filename[0] == '/':
            filename = filename[1:]
        self._load_file(gcmd, filename, check_subdirs=True)
        self.do_resume()
    def cmd_M20(self, gcmd):
        # List SD card
        files = self.get_file_list()
        gcmd.respond_raw("Begin file list")
        for fname, fsize in files:
            gcmd.respond_raw("%s %d" % (fname, fsize))
        gcmd.respond_raw("End file list")
    def cmd_M21(self, gcmd):
        # Initialize SD card
        gcmd.respond_raw("SD card ok")
    def cmd_M23(self, gcmd):
        # Select SD file
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get_raw_command_parameters().strip()
        if filename.startswith('/'):
            filename = filename[1:]
        self._load_file(gcmd, filename)
    def _load_file(self, gcmd, filename, check_subdirs=False):
        files = self.get_file_list(check_subdirs)
        flist = [f[0] for f in files]
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        fname = filename
        try:
            if fname not in flist:
                fname = files_by_lower[fname.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = io.open(fname, 'r', newline='')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except:
            logging.exception("virtual_sdcard file open")
            raise gcmd.error("Unable to open file")
        gcmd.respond_raw("File opened:%s Size:%d" % (filename, fsize))
        gcmd.respond_raw("File selected")
        self.current_file = f
        self.original_file_path = f.name
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
        self._handle_file_caching(gcmd, fname, f)
    def cmd_M24(self, gcmd):
        # Start/resume SD print
        self.do_resume()
    def cmd_M25(self, gcmd):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, gcmd):
        # Set SD position
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        pos = gcmd.get_int('S', minval=0)
        self.file_position = pos
    def cmd_M27(self, gcmd):
        # Report SD print status
        if self.current_file is None:
            gcmd.respond_raw("Not SD printing.")
            return
        gcmd.respond_raw("SD printing byte %d/%d"
                         % (self.file_position, self.file_size))
    def get_file_position(self):
        return self.next_file_position
    def get_file_line(self):
        return self.file_line
    def set_file_position(self, pos):
        self.next_file_position = pos
    def is_cmd_from_sd(self):
        return self.cmd_from_sd
    # Background work timer
    def work_handler(self, eventtime):
        logging.info("Starting SD card print (position %d)", self.file_position)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            return self.reactor.NEVER
        self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        error_message = None
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self.current_file.read(8192)
                except:
                    logging.exception("virtual_sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    self._cleanup_cache(clean_files=True)
                    logging.info("Finished SD card print")
                    self.gcode.respond_raw("Done printing file")
                    break
                lines = data.split('\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            line = lines.pop()
            self.file_line += 1
            if sys.version_info.major >= 3:
                next_file_position = self.file_position + len(line.encode()) + 1
            else:
                next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            try:
                self.gcode.run_script(line)
            except self.gcode.error as e:
                error_message = str(e)
                try:
                    self.gcode.run_script(self.on_error_gcode.render())
                except:
                    logging.exception("virtual_sdcard on_error")
                break
            except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
            # Do we need to skip around?
            if self.next_file_position != next_file_position:
                try:
                    self.current_file.seek(self.file_position)
                except:
                    logging.exception("virtual_sdcard seek")
                    self.work_timer = None
                    return self.reactor.NEVER
                lines = []
                partial_input = ""
        logging.info("Exiting SD card print (position %d)", self.file_position)
        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
        return self.reactor.NEVER

def load_config(config):
    return VirtualSD(config)

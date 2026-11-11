# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import bisect, os, sys, logging, io
import chelper

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
        # Deferred pause bookkeeping: the runline -> byte-offset lookup reads the
        # g-code file and must NOT run between the freeze and the park moves.
        self._pause_lookup_timer = None
        self._pending_pause_lookup = None
        # 50 Hz flush of the MCU step queues while paused (see _start_step_keeper)
        self._keeper_timer = None
        self._pause_started_at = None
        # Power-loss recovery: save the resume point periodically.
        self.plr_save_s = config.getfloat('plr_save_s', 15., minval=0.)
        self._plr_timer = None
        self._plr_armed = False
        # Set when _anchor_time_base() lowers toolhead.start_buffer_time, so
        # do_resume() can put the stock value back.
        self._orig_start_buffer = None
        # Sparse index of (gcode line number, byte offset of that line's start).
        # Lets the quick-pause resume point be resolved without rescanning the
        # whole file - prints up to 1GB are read from this sdcard.
        self._line_index = []
        self._line_index_stride = 1024
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
        self.gcode.register_command(
            'POWER_LOSS_RETRACT', self.cmd_POWER_LOSS_RETRACT,
            desc=self.cmd_POWER_LOSS_RETRACT_help)
        self.gcode.register_command(
            'POWER_LOSS_MARK', self.cmd_POWER_LOSS_MARK,
            desc=self.cmd_POWER_LOSS_MARK_help)
        self.gcode.register_command(
            'QUICK_PAUSE_SNAP', self.cmd_QUICK_PAUSE_SNAP,
            desc=self.cmd_QUICK_PAUSE_SNAP_help)
        self.gcode.register_command(
            'START_RESUME_TOAST', self.cmd_START_RESUME_TOAST,
            desc=self.cmd_START_RESUME_TOAST_help)
        self.gcode.register_command(
            'STOP_RESUME_TOAST', self.cmd_STOP_RESUME_TOAST,
            desc=self.cmd_STOP_RESUME_TOAST_help)
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
    def do_pause(self, capture=True):
        self._pause_started_at = self.reactor.monotonic()
        if self.work_timer is not None:
            self.must_pause_work = True
            t0 = self.reactor.monotonic()
            if capture:
                self._get_runline(for_pause=True)
            t1 = self.reactor.monotonic()
            self._quick_pause_continue()
            t2 = self.reactor.monotonic()
            self._set_quick_pause()
            t3 = self.reactor.monotonic()
            logging.info("quick pause timing: runline=%.0fms window=%.0fms "
                         "freeze_chain=%.0fms -> frozen",
                         (t1 - t0) * 1000., (t2 - t1) * 1000.,
                         (t3 - t2) * 1000.)
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
            logging.info("quick pause timing: work handler exited "
                         "%.0fms after freeze", (self.reactor.monotonic() - t3) * 1000.)
            # The line-offset lookup below READS THE G-CODE FILE.  Doing it right
            # here puts file I/O between the freeze (which has just re-anchored
            # print_time to mcu_now + pause_anchor_ms) and the park moves the
            # PAUSE macro is about to queue - and a slow eMMC read there is
            # exactly what produced "Timer too close" / "Stepper too far in past"
            # (2026-10-20/21).  The printer is paused and idle now, so defer it:
            # the timer runs half a second later and do_resume() forces it if the
            # user resumes sooner.
            # NO timer: the lookup (and the exact-resume planning) reads the
            # g-code file several times, and doing that while the park moves are
            # still executing starved the MCU ("Timer too close", 2026-10-23).
            # do_resume() runs it later, when the machine is idle.
            self._pending_pause_lookup = capture

    def _start_step_keeper(self):
        """Keep feeding the MCU step queues while the print is paused.

        While printing, every dispatched line runs toolhead.flush_step_generation()
        through the work handler.  That handler stops when the pause freezes the
        print, so the retreat moves queued afterwards are only partially turned
        into MCU step commands - anf the MCUs then run dry and answer "Timer too
        close" -> "Stepper too far in past" (2026-10-21, right after a pause).
        This timer keeps flushing at 50 Hz until the resume/cancel stops it.
        """
        try:
            self._stop_step_keeper()
            self._keeper_timer = self.reactor.register_timer(
                self._keeper, self.reactor.monotonic() + 0.02)
        except Exception:
            logging.exception("quick pause: cannot start the step keeper")

    def _stop_step_keeper(self):
        timer = getattr(self, '_keeper_timer', None)
        if timer is not None:
            try:
                self.reactor.update_timer(timer, self.reactor.NEVER)
            except Exception:
                pass
            self._keeper_timer = None

    def _keeper(self, eventtime):
        try:
            toolhead = self.printer.lookup_object('toolhead', None)
            if toolhead is not None:
                toolhead.flush_step_generation()
        except Exception:
            logging.exception("quick pause: step keeper flush failed")
        # Keep the hold time current: the resume macro reads it while rendering,
        # which happens before do_resume() runs.
        try:
            started = getattr(self, '_pause_started_at', None)
            if started is not None:
                macro = self.printer.lookup_object('gcode_macro RESUME', None)
                if macro is not None:
                    macro.variables['pause_hold_s'] = max(
                        0.0, self.reactor.monotonic() - started)
        except Exception:
            logging.exception("quick pause: cannot refresh the hold time")
        return eventtime + 0.02

    def _peek_line_words(self, line_no):
        """Parse a line's X/Y/E words.  Returns dict or None when unsure."""
        try:
            pos = self._get_position_by_line(line_no, quiet=True)
            if pos is None:
                return None
            self.current_file.seek(pos)
            raw = self.current_file.readline()
            if not raw:
                return None
            line = raw.split(';')[0].strip()
            low = line.upper()
            if not low.startswith(('G0', 'G1')):
                return None
            if 'Z' in low:
                return None
            out = {}
            for word in ('X', 'Y', 'E'):
                i = low.find(word)
                if i < 0:
                    continue
                try:
                    out[word] = float(low[i + 1:].split()[0])
                except Exception:
                    return None
            return out
        except Exception:
            return None

    def _apply_resume_position(self, position, target_line):
        """Set the resume point in the file (idempotent w.r.t. work_handler)."""
        self.file_position = position
        self.file_line = target_line - 1
        # work_handler assigns self.file_position = self.next_file_position right
        # after the line it is currently dispatching returns.  When this pause was
        # raised from inside that dispatch (runout -> PAUSE, or a PAUSE line in the
        # gcode) that assignment would overwrite the resume point, so keep it
        # idempotent.
        self.next_file_position = position

    def _plan_exact_resume(self, line_no):
        """Synthetic command that finishes the interrupted segment, or None."""
        frozen = getattr(self, 'toolhead_pos', None)
        if frozen is None:
            return None
        cur = self._peek_line_words(line_no)
        if cur is None or 'X' not in cur or 'Y' not in cur or 'E' not in cur:
            return None
        prev = self._peek_line_words(line_no - 1) if line_no > 1 else None
        if prev is None or 'X' not in prev or 'Y' not in prev:
            return None
        try:
            absolute_e = bool(self.printer.lookup_object(
                'gcode_move').absolute_extrude)
        except Exception:
            return None
        sx, sy = prev['X'], prev['Y']
        ex, ey = cur['X'], cur['Y']
        seg = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
        if seg < 1e-6:
            return None
        # project the frozen position onto the segment: it must lie ON it (small
        # perpendicular distance) and inside it, otherwise the geometry does not
        # match and the caller falls back to replay/skip.
        ux, uy = (ex - sx) / seg, (ey - sy) / seg
        dx, dy = frozen[0] - sx, frozen[1] - sy
        proj = dx * ux + dy * uy
        perp = abs(dx * uy - dy * ux)
        if perp > 0.25 or proj < -0.05 or proj > seg + 0.05:
            return None
        frac = (seg - proj) / seg
        if frac <= 0.002 or frac >= 0.998:
            return None
        if absolute_e:
            e_cmd = 'E%.5f' % cur['E']
        else:
            e_cmd = 'E%.5f' % (cur['E'] * frac)
        return 'G1 X%.3f Y%.3f %s' % (ex, ey, e_cmd)

    def _peek_line_end(self, line_no):
        """End point (x, y) of line `line_no`, or None if it can not be read.

        Only absolute X/Y are trusted; relative moves / arcs / malformed lines
        return None so the caller falls back to replaying the line.
        """
        try:
            pos = self._get_position_by_line(line_no, quiet=True)
            if pos is None:
                return None
            self.current_file.seek(pos)
            line = self.current_file.readline()
            if not line or '(' in line or ';' in line.split('X')[0]:
                return None
            low = line.upper()
            if low.lstrip().startswith(('G2', 'G3')):
                return None
            # A line carrying Z must never be skipped: the pause freezes at the
            # measured (possibly half-way) Z, so dropping the line would lose its Z
            # destination and the following layers would run at the wrong height.
            if 'Z' in low:
                return None
            xs = line.find('X')
            ys = line.find('Y')
            if xs < 0 or ys < 0:
                return None
            x = float(line[xs + 1:].split()[0])
            y = float(line[ys + 1:].split()[0])
            return (x, y)
        except Exception:
            return None

    def _finish_pause_lookup(self, eventtime):
        self._pause_lookup_timer = None
        capture = self._pending_pause_lookup
        self._pending_pause_lookup = None
        if not capture:
            return self.reactor.NEVER
        try:
            if self.file_runline > 0 and self.current_file is not None:
                # Resume at the line AFTER the one that was interrupted.  The pause
                # freezes the machine in the MIDDLE of that segment, so replaying it
                # would extrude the whole segment's E over the remaining half and
                # leave a small pile at the resume point (measured 2026-10-23).
                # Skipping it loses at most ~0.5 mm of wall, which is far less
                # visible than the pile.
                ##### conservative exact resume: if the interrupted line is a #####
                ##### plain straight XY+E move we complete just its remainder.   #####
                synthetic = self._plan_exact_resume(self.file_runline)
                if synthetic is not None:
                    self._resume_synthetic = synthetic
                    logging.info("quick pause: exact resume move '%s'", synthetic)
                    position = self._get_position_by_line(self.file_runline + 1)
                    if position is not None:
                        self._apply_resume_position(position,
                                                    self.file_runline + 1)
                        return self.reactor.NEVER
                target_line = self.file_runline + 1
                # Guard: only skip the interrupted line when the REST of it is
                # short.  A slicer normally splits long moves into small segments
                # (~0.4 mm here), but a straight infill line or a G2/G3 arc can be
                # ONE long command - skipping that would remove a whole stretch of
                # the part.  In that case replay it (worst case a small pile).
                skip_max = 2.0
                skip = True
                frozen = getattr(self, 'toolhead_pos', None)
                if frozen is not None:
                    end = self._peek_line_end(self.file_runline)
                    if end is None:
                        skip = False
                    else:
                        dx = end[0] - frozen[0]
                        dy = end[1] - frozen[1]
                        if (dx * dx + dy * dy) ** 0.5 > skip_max:
                            skip = False
                if not skip:
                    target_line = self.file_runline
                # work_handler increments file_line before dispatching a line, so
                # the line counter must be one less than the line being resumed
                # at, otherwise taskline is off by one
                position = self._get_position_by_line(target_line)
                if position is not None:
                    self._apply_resume_position(position, target_line)
        except Exception:
            logging.exception("quick pause: deferred runline lookup failed")
        return self.reactor.NEVER
    def do_resume(self):
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self._stop_step_keeper()
        # Publish how long the print was paused: the make-up prime at the
        # resume scales with it (a hot nozzle keeps oozing while parked, so a
        # longer pause needs more filament pushed back before the first line).
        hold = 0.0
        try:
            started = getattr(self, '_pause_started_at', None)
            if started is not None:
                hold = max(0.0, self.reactor.monotonic() - started)
                macro = self.printer.lookup_object('gcode_macro RESUME', None)
                if macro is not None:
                    macro.variables['pause_hold_s'] = hold
        except Exception:
            logging.exception('quick pause: cannot publish the hold time')
        logging.info('quick pause: hold time %.1f s', hold)
        if getattr(self, '_pending_pause_lookup', None):
            self._finish_pause_lookup(None)
        ##### exact resume: finish the interrupted segment before the file #####
        ##### continues.  This MUST come after the lookup above, because the #####
        ##### lookup is what builds the command (doing it the other way round #####
        ##### silently skipped the interrupted segment -> missing material in  #####
        ##### the first lines, 2026-10-23).                                    #####
        syn = getattr(self, '_resume_synthetic', None)
        self._resume_synthetic = None
        if syn:
            try:
                logging.info("quick pause: completing interrupted segment with '%s'",
                             syn)
                self.gcode.run_script_from_command(syn)
            except Exception:
                logging.exception("quick pause: exact resume move failed")
        # Logged before the motion defaults are restored: the resume macro has
        # already run its unretract + extra prime at this point.
        self._log_e_state("resume")
        # The macro keeps extruding after RESUME_BASE returns (the part prime),
        # so sample the E bookkeeping again a moment later - that is the only
        # way to tell whether the "prime at the part" really fed filament.
        self._schedule_e_log("resume+1s", 1.0)
        # The pause path may have lowered toolhead.start_buffer_time; the
        # resumed print must get the stock lead time back.
        self._restore_motion_defaults()
        self._plr_arm()
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        self.gcode.request_cancel()
        if self.current_file is not None:
            # The print is being discarded, so the resume point is not needed -
            # skip the line lookup but still stop the steppers immediately.
            self._plr_disarm()
            self.do_pause(capture=False)
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self._cleanup_cache(clean_files=False)
        self.file_position = self.file_size = 0
        self.file_line = self.file_runline = 0
        # Drop any deferred pause bookkeeping: there is nothing to resume.
        self._stop_step_keeper()
        self._pending_pause_lookup = None
        if getattr(self, '_pause_lookup_timer', None) is not None:
            self.reactor.update_timer(self._pause_lookup_timer,
                                      self.reactor.NEVER)
            self._pause_lookup_timer = None
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    # ---- power-loss recovery: periodic resume-point save ------------------
    def _plr_arm(self):
        """Mark the print as interruptible and (re)start the periodic save."""
        if self.plr_save_s <= 0.:
            return
        self._plr_armed = True
        self._plr_save(force_path=True)
        self._plr_stop_timer()
        try:
            self._plr_timer = self.reactor.register_timer(
                self._plr_tick, self.reactor.monotonic() + self.plr_save_s)
        except Exception:
            logging.exception("plr: cannot start the save timer")

    def _plr_disarm(self):
        if not self._plr_armed:
            return
        self._plr_armed = False
        self._plr_stop_timer()
        try:
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=was_interrupted VALUE=False")
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=power_resume_line VALUE=0")
        except Exception:
            logging.exception("plr: cannot disarm")

    def _plr_stop_timer(self):
        if self._plr_timer is not None:
            try:
                self.reactor.update_timer(self._plr_timer, self.reactor.NEVER)
            except Exception:
                pass
            self._plr_timer = None

    def _plr_save(self, force_path=False):
        """Write the resume point (2 SAVE_VARIABLE calls ~ 40 ms)."""
        try:
            # file_runline only exists once the stepper tasklines were read
            # (GET_TASKLINE / pause); file_line advances with every dispatched
            # line and is the only value available while printing normally.
            runline = self.file_runline or self.file_line
            if runline <= 0:
                return
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=was_interrupted VALUE=True")
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=power_resume_line VALUE=%d" % runline)
            if force_path:
                try:
                    path = self.current_file.name if self.current_file else ''
                except Exception:
                    path = ''
                # Never point filepath at the PLR resume copy: it must stay on the
                # original g-code, otherwise the next outage would slice the copy.
                if path and '/.plr/' not in path and not path.endswith('.plr'):
                    # SAVE_VARIABLE requires a Python literal: send VALUE="'path'"
                    # (a single-quoted literal).  A bare quoted path is unquoted by
                    # the g-code parser and literal_eval then fails with
                    # "Malformed command" (reported 2026-10-28).
                    safe = path.replace('\\', '/').replace("'", "\\'")
                    self.gcode.run_script_from_command(
                        "SAVE_VARIABLE VARIABLE=filepath VALUE=\"'%s'\"" % safe)
        except Exception:
            logging.exception("plr: save failed")

    def _plr_tick(self, eventtime):
        if not self._plr_armed:
            return self.reactor.NEVER
        if not self.is_active():
            return eventtime + self.plr_save_s
        self._plr_save()
        return eventtime + self.plr_save_s

    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause(capture=False)
            self.current_file.close()
            self.current_file = None
        self.original_file_path = None
        self.file_position = self.file_size = 0
        self.file_line = self.file_runline = 0
        self._line_index = []
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
    def _get_runline(self, for_pause=False):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise self.gcode.error("Printer not ready")
        kin = toolhead.get_kinematics()
        steppers = kin.get_steppers()
        if for_pause:
            # Quick pause: earliest G-code line that still has moves pending on
            # any stepper.  Resuming from there replays a little, but never skips
            # a segment.
            linfo = [(s.get_name(), s.get_stepper_taskline()) for s in steppers]
            positions = [position for _, position in linfo if position != 0]
            runline = min(positions) if positions else 0
        else:
            # Power loss recovery: keep the historical behaviour - latest
            # pending line, Z steppers excluded.
            linfo = [(s.get_name(), s.get_stepper_taskline()) for s in steppers
                     if s.get_name() != "stepper_z"]
            positions = [position for _, position in linfo]
            runline = max(positions) if positions else 0
        self.linfo = linfo
        self.file_runline = runline
        logging.info("stepper tasklines=%s -> runline=%d (for_pause=%s)",
                     linfo, runline, for_pause)

    def _get_pause_retract(self):
        """Retract length (mm) executed inside the pause_continue window.

        Comes from _CLIENT_VARIABLE.pause_retract; 0 disables the in-window
        retract (only the printing extrusion gets truncated)."""
        try:
            macro = self.printer.lookup_object(
                'gcode_macro _CLIENT_VARIABLE', None)
            if macro is not None:
                val = macro.get_status(0).get('pause_retract')
                if val is not None:
                    return max(0., float(val))
        except Exception:
            pass
        return 10.

    def _log_e_state(self, tag):
        """quick pause: log the whole E bookkeeping in one line.

        A single pause then proves whether the retract / unretract / extra prime
        are accounted for: `commanded` is the toolhead E coordinate (what the
        trapq is driven with), `base`/`gcode` are gcode_move's, and
        `filament_used` is the print odometer (sum of positive E moves)."""
        try:
            toolhead = self.printer.lookup_object('toolhead', None)
            if toolhead is None:
                return
            base = gpos = float('nan')
            spd = sfac = float('nan')
            gm = self.printer.lookup_object('gcode_move', None)
            if gm is not None:
                st = gm.get_status(0)
                # gcode_move.get_status() exposes 'position' (= its
                # last_position, i.e. the toolhead E) and 'gcode_position';
                # base_position is not exported, but gcode = last - base.
                lpos = st['position'][3]
                gpos = st['gcode_position'][3]
                base = lpos - gpos
                # The gcode file relies on the modal feedrate: its
                # "G1 X.. Y.. E.." lines carry no F, so any pause/resume move
                # that leaves a different F behind would change the whole
                # resumed print speed (that is exactly what happened on
                # 2026-10-20: a prime after RESUME_BASE left speed=600).
                spd = st.get('speed', float('nan'))
                sfac = st.get('speed_factor', float('nan'))
            used = float('nan')
            ps = getattr(self, 'print_stats', None)
            if ps is not None:
                used = ps.get_status(0).get('filament_used', float('nan'))
            if sfac == sfac and sfac:       # speed_factor is exported as M220*60
                sfac = sfac / 60.
            logging.info("quick pause E %-10s commanded=%.4f base=%.4f "
                         "gcode=%.4f filament_used=%.4f feed=%.1fmm/s m220=%.2f "
                         "pos=%.3f,%.3f,%.3f",
                         tag, toolhead.commanded_pos[3], base, gpos, used,
                         spd, sfac, toolhead.commanded_pos[0],
                         toolhead.commanded_pos[1], toolhead.commanded_pos[2])
        except Exception:
            logging.exception("quick pause: E accounting log failed")

    def _schedule_e_log(self, tag, delay):
        """Log the E bookkeeping once, `delay` seconds from now."""
        try:
            def _cb(eventtime):
                self._log_e_state(tag)
                return self.reactor.NEVER
            self.reactor.register_timer(_cb,
                                        self.reactor.monotonic() + delay)
        except Exception:
            logging.exception("quick pause: cannot schedule the E log")

    def _get_extruders(self):
        extruders = []
        for i in range(99):
            name = "extruder" if i == 0 else "extruder%d" % (i,)
            extruder = self.printer.lookup_object(name, None)
            if extruder is None:
                break
            extruders.append(extruder)
        return extruders

    def _get_pause_anchor_ms(self):
        """Lead time (ms) granted to the host for the retreat moves.

        [virtual_sdcard] pause_anchor_ms, default 150.  0 disables the
        re-anchoring, in which case the retreat inherits the stale ~2.2 s
        look-ahead lead time (this was the real cause of the "pause hitch")."""
        ms = getattr(self, 'pause_anchor_ms', None)
        if ms is None:
            ms = 150
            try:
                settings = self.printer.lookup_object(
                    'configfile').get_status(0)['settings']
                raw = settings.get('virtual_sdcard', {}).get('pause_anchor_ms')
                if raw is not None:
                    ms = int(float(raw))
            except Exception:
                ms = 150
        try:
            return max(0, min(int(ms), 1000))
        except Exception:
            return 150

    def _anchor_time_base(self, anchor_s):
        """Pull the host motion timeline back to "mcu now + anchor".

        Freezing with stepper_stop() only empties the *mcu* queue; the host
        timeline (toolhead.print_time) still points at the end of the plan that
        was just thrown away - measured 2.17 s in the future on this machine.
        _calc_print_time() only ever pushes print_time forward, so every retreat
        move queued afterwards would inherit that stale lead time and the nozzle
        would sit on the part for the whole look-ahead depth.  Re-anchoring is
        what makes the retreat start right away; the flush / step generation
        cursors must be pulled back with it, otherwise trapq_finalize_moves()
        treats the freshly queued moves as expired.
        """
        toolhead = self.printer.lookup_object('toolhead')
        now = self.reactor.monotonic()
        est = toolhead.mcu.estimated_print_time(now)
        t_anchor = est + anchor_s
        if toolhead.print_time <= t_anchor:
            logging.info("quick pause: anchor not needed (print_time=%.3f <= "
                         "anchor=%.3f est=%.3f)", toolhead.print_time,
                         t_anchor, est)
            return t_anchor
        old = toolhead.print_time
        if self._orig_start_buffer is None:
            self._orig_start_buffer = toolhead.start_buffer_time
        toolhead.print_time = t_anchor
        toolhead.last_flush_time = min(toolhead.last_flush_time, t_anchor)
        toolhead.min_restart_time = min(toolhead.min_restart_time, t_anchor)
        toolhead.step_gen_time = min(toolhead.step_gen_time, t_anchor)
        toolhead.need_flush_time = min(toolhead.need_flush_time, t_anchor)
        # Keep _calc_print_time()'s lead above Klipper's stock buffer.  The anchor
        # itself still pulls print_time back to est+anchor (that is what makes the
        # retreat start immediately instead of after the old look-ahead), but
        # every move queued *afterwards* must keep a safety margin: with
        # start_buffer_time == anchor_s a single host hiccup in that window makes
        # the park moves be scheduled in the past and the MCU reports "Timer too
        # close" -> shutdown (2026-10-20/21, repeatedly).
        # 0.25 s (stock) proved too tight on this machine; 0.4 s is the compromise
        # the supervisor asked for - it costs 150 ms of pause latency, not the
        # 250 ms of a 0.5 s buffer.
        safe_buffer = max(self._orig_start_buffer or 0., 0.4)
        toolhead.start_buffer_time = max(anchor_s, safe_buffer)
        logging.info("quick pause: anchor print_time %.3f -> %.3f (est=%.3f, "
                     "cut %.0f ms of look-ahead, buffer %.3f)",
                     old, t_anchor, est, (old - t_anchor) * 1000.,
                     toolhead.start_buffer_time)
        # Probe: PURE DIAGNOSTIC.  t_anchor lives in the *print time* domain while
        # reactor timers use monotonic time, so it has to be converted (an earlier
        # version registered the print-time value directly and the timer fired at
        # once, which made the reported numbers meaningless).
        def _probe(eventtime):
            try:
                now2 = self.reactor.monotonic()
                est2 = toolhead.mcu.estimated_print_time(now2)
                logging.info("quick pause: anchor probe lead=%.3fs "
                             "timer_late=%.3fs print_time=%.3f est=%.3f",
                             toolhead.print_time - est2, now2 - (now + anchor_s),
                             toolhead.print_time, est2)
            except Exception:
                logging.exception("quick pause: anchor probe failed")
            return self.reactor.NEVER
        try:
            self.reactor.register_timer(_probe, now + anchor_s)
        except Exception:
            logging.exception("quick pause: cannot register the anchor probe")
        self._start_step_keeper()
        return t_anchor

    def _restore_motion_defaults(self):
        try:
            toolhead = self.printer.lookup_object('toolhead', None)
            if toolhead is not None and self._orig_start_buffer is not None:
                toolhead.start_buffer_time = self._orig_start_buffer
                self._orig_start_buffer = None
        except Exception:
            pass

    def _retract_profile(self, dist, window, max_v, max_a):
        """Symmetric trapezoid that retracts `dist` mm in `window` seconds.

        Returns (accel_t, cruise_t, decel_t, cruise_v, accel, duration).
        The previous version appended the move with start_v == cruise_v and
        accel == 0, i.e. it stepped the e-motor from 0 to ~60 mm/s in one
        step - an impulse on the extruder.  This solves for a proper
        trapezoid: a symmetric profile covers dist = cruise_v * (accel_t +
        cruise_t) and is back at rest when decel_t == accel_t.

        `window` is a *maximum*; when the distance cannot be covered inside it
        without breaking max_e_velocity / max_e_accel the window is stretched
        and the caller uses the returned duration as the freeze point.
        """
        if dist <= 0. or window <= 0. or max_v <= 0. or max_a <= 0.:
            return 0., 0., 0., 0., 0., 0.
        # Shortest possible duration for this distance under both limits.
        cruise_dist = max_v * max_v / max_a
        if dist >= cruise_dist:
            t_min = dist / max_v + max_v / max_a
        else:
            t_min = 2. * (dist / max_a) ** 0.5
        duration = max(window, t_min)
        # Gentlest ramp that still fits `duration`: solves a*(T-a) = dist/max_a
        disc = duration * duration - 4. * dist / max_a
        if disc <= 0.:
            accel_t = duration * 0.5
        else:
            accel_t = (duration - disc ** 0.5) * 0.5
        cruise_t = max(duration - 2. * accel_t, 0.)
        span = accel_t + cruise_t
        if span <= 0.:
            return 0., 0., 0., 0., 0., 0.
        cruise_v = dist / span
        accel = cruise_v / accel_t if accel_t > 0. else 0.
        return accel_t, cruise_t, accel_t, cruise_v, accel, 2. * accel_t + cruise_t

    def _quick_pause_continue(self):
        """quick pause: let the planned moves play out and retract the
        filament *for real* before the queue is frozen.

        Two facts drive this implementation:

        * trapq_finalize_moves() does NOT truncate future moves - it only
          expires finished ones (see chelper/trapq.c).  So the old
          "trapq_finalize_moves(tq, target)" never cut the motion at `target`;
          the only thing that stops the toolhead is the firmware
          `stepper_stop` sent by _set_quick_pause().  Waiting for the planned
          moves to finish (instead of cutting them off after a fixed 250 ms)
          is what makes the stop smooth rather than a full-speed hard stop.
        * A retract move hand-appended to the extruder trapq is never flushed
          by the normal stepping code (flush_step_generation() clamps at
          print_time - kin_flush_delay), and _rebase_extruder_position() then
          deletes it from the trapq - the old in-window retract therefore
          never reached the motor.  We now push the flush head over the window
          ourselves (_advance_move_time / _advance_flush_time, the same calls
          the normal move planner uses) and wait until the MCU has really
          executed it, so the nozzle is relieved *before* the freeze.

        pause_continue_ms is the maximum length of that window (0 disables the
        retract; see _get_pause_retract).

        IMPORTANT (measured 2026-10-19): the look-ahead buffer on this machine
        is ~2.2 s deep while printing (pause_t - est_print_time), so a retract
        appended at pause_t can only reach the motor ~2.2 s after PAUSE.  For
        the *fast* pause the default is therefore to not use this window at
        all (pause_wait_mcu_ms = 0) and to lift/retract from the park macro
        instead; set pause_wait_mcu_ms > 0 under [virtual_sdcard] to get the
        "keep printing and retract before the stop" behaviour (smoother, but
        the pause only takes effect after the buffer has played out)."""
        ms = getattr(self, 'pause_continue_ms', None)
        wait_ms = getattr(self, 'pause_wait_mcu_ms', None)
        if ms is None or wait_ms is None:
            settings = {}
            try:
                settings = self.printer.lookup_object(
                    'configfile').get_status(0)['settings'].get(
                        'virtual_sdcard', {})
            except Exception:
                settings = {}
            if ms is None:
                ms = 200
                raw = settings.get('pause_continue_ms')
                if raw is not None:
                    ms = int(float(raw))
            if wait_ms is None:
                wait_ms = 0
                raw = settings.get('pause_wait_mcu_ms')
                if raw is not None:
                    wait_ms = int(float(raw))
        try:
            ms = max(0, min(int(ms), 500))
            wait_ms = max(0, min(int(wait_ms), 5000))
        except Exception:
            ms = 0
            wait_ms = 0
        if ms <= 0 or wait_ms <= 0:
            # Fast path: freeze immediately, the park macro does the retract.
            logging.info("quick pause: window DISABLED (ms=%d wait_ms=%d)",
                         ms, wait_ms)
            return
        max_wait = wait_ms / 1000.

        try:
            toolhead = self.printer.lookup_object('toolhead')
            reactor = self.reactor
            # chelper.get_ffi() returns the (main, lib) tuple - the trapq_*
            # helpers live on the *lib* object, so take element [1].
            ffi = getattr(self, 'ffi_lib', None) or chelper.get_ffi()[1]
            ms_s = ms / 1000.
            pause_t = toolhead.get_last_move_time()
            end_t = pause_t
            n_ret = 0
            retract = self._get_pause_retract()
            active = toolhead.get_extruder()
            if retract > 0. and ms_s > 0. and active is not None:
                max_v = abs(float(getattr(active, 'max_e_velocity', 60.)))
                max_a = abs(float(getattr(active, 'max_e_accel', 1000.)))
                if max_v <= 0.:
                    max_v = 60.
                if max_a <= 0.:
                    max_a = 1000.
                accel_t, cruise_t, decel_t, cruise_v, accel, dur = \
                    self._retract_profile(retract, ms_s, max_v, max_a)
                if dur > 0.:
                    e_now = float(getattr(active, 'last_position', 0.))
                    # trapq_append(tq, print_time, accel_t, cruise_t, decel_t,
                    #              x, y, z, x_r, y_r, z_r,
                    #              start_v, cruise_v, accel, taskline)
                    # (-1 in the x direction retracts; x is the E dimension,
                    #  y carries the pressure-advance flag -> 0 here)
                    ffi.trapq_append(active.get_trapq(), pause_t,
                                     accel_t, cruise_t, decel_t,
                                     e_now, 0., 0.,
                                     -1., 0., 0.,
                                     0., cruise_v, accel, self.file_runline)
                    end_t = pause_t + dur
                    n_ret = 1
                    logging.info(
                        "quick pause: retract %.2f mm in %.0f ms "
                        "(ramp %.0f ms, v=%.1f mm/s, a=%.0f mm/s^2)",
                        retract, dur * 1000., accel_t * 1000., cruise_v, accel)
            if end_t > pause_t:
                # Generate and transmit those steps exactly like the move
                # planner does.  _advance_move_time() advances print_time and
                # flushes in MOVE_BATCH_TIME slices; the trailing
                # kin_flush_delay + STEPCOMPRESS_FLUSH_TIME is deliberately not
                # flushed by it, so close that gap explicitly - otherwise the
                # tail of the retract would still be dropped by the freeze.
                toolhead._advance_move_time(end_t)
                toolhead._advance_flush_time(end_t)
            else:
                # No retract configured: nothing to add, just let the planned
                # moves finish (no artificial extra travel).
                logging.info("quick pause: no in-window retract, "
                             "letting the plan drain")
            est_t = toolhead.mcu.estimated_print_time(reactor.monotonic())
            wait = end_t - est_t + 0.020
            wait = min(max(wait, 0.), max_wait)
            logging.info("quick pause: window %.0f ms retract=%d "
                         "pause_t=%.3f flush=%.3f est=%.3f wait=%.3f",
                         ms, n_ret, pause_t, end_t, est_t, wait)
            if wait > 0.:
                reactor.pause(reactor.monotonic() + wait)
            est_t = toolhead.mcu.estimated_print_time(reactor.monotonic())
            logging.info("quick pause: window done, est=%.3f (target %.3f)",
                         est_t, end_t)
        except Exception:
            logging.exception("quick pause: continue-moves failed, "
                              "freezing immediately")

    def _set_quick_pause(self):
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.reactor
        # Guard: the freeze/rebuild below rewrites the step clock, which is only
        # meaningful while the machine is actually printing along a homed
        # trajectory.  With no homed axes (PAUSE pressed during G28, or after a
        # cancelled home) there is nothing valid to freeze, and the surgery
        # desynchronises the step clock badly enough that the MCU answers
        # "Timer too close" and shuts down (2026-10-20 16:10).  Fall back to the
        # plain pause in that case.
        homed = getattr(toolhead, 'homed_axes', None)
        if homed is None:
            try:
                homed = toolhead.get_status(reactor.monotonic()).get(
                    'homed_axes', '')
            except Exception:
                homed = ''
        if 'xyz' not in homed:
            logging.info("quick pause: freeze SKIPPED (homed_axes='%s', no valid "
                         "trajectory to freeze)", homed)
            return
        kin = toolhead.get_kinematics()
        t0 = reactor.monotonic()
        toolhead.flush_step_generation()
        t1 = reactor.monotonic()
        steppers = kin.get_steppers()
        extruders = self._get_extruders()
        for extruder in extruders:
            extruder.extruder_stepper.stepper.set_stepper_stop()
        for stepper in steppers:
            stepper.set_stepper_stop()
        t2 = reactor.monotonic()
        # Read the actual position of *every* extruder: the active one is
        # synced into the G-code E coordinate, all of them are re-based below.
        # Only re-basing the active extruder left the other one with a stale
        # trapq, which makes a later tool change jump (silently retracting on
        # the wrong motor).
        active_extruder = toolhead.get_extruder()
        e_positions = []
        for extruder in extruders:
            estepper = extruder.extruder_stepper.stepper
            pause_pos = estepper.set_stepper_pause()
            e_positions.append((extruder,
                                estepper.mcu_to_commanded_position(pause_pos)))
        t3 = reactor.monotonic()
        e_pos = None
        for extruder, pos in e_positions:
            if extruder is active_extruder:
                e_pos = pos
        kin_spos = {
            stepper.get_name(): stepper.mcu_to_commanded_position(stepper.set_stepper_pause())
            for stepper in steppers
        }
        self.toolhead_pos = kin.calc_position(kin_spos)
        logging.info("quick pause capture: taskline=%d linfo=%s pos=%s e=%s",
                     self.file_runline, self.linfo, self.toolhead_pos, e_pos)
        # Move the host timeline back to "mcu now + anchor" *before* re-basing,
        # so the trapq markers and the retreat moves below are all placed on the
        # new (short) lead time instead of the discarded ~2.2 s look-ahead.
        anchor_ms = self._get_pause_anchor_ms()
        if anchor_ms > 0:
            try:
                self._anchor_time_base(anchor_ms / 1000.)
            except Exception:
                logging.exception("quick pause: anchor failed, keeping the "
                                  "old timeline")
        if e_pos is not None:
            self.toolhead_pos = list(self.toolhead_pos) + [e_pos]
            # Sync extruder itersolve position; _is_pause_stop keeps the
            # mcu position offset untouched
            for extruder, pos in e_positions:
                extruder.extruder_stepper.stepper.set_position([pos, 0., 0.])
                # The extruder stepper is driven from its own trapq, and
                # toolhead.set_position() only re-bases the toolhead's trapq.
                # Without re-basing the extruder's trapq and its cached
                # last_position, the next E move is generated from the stale
                # scheduled position, which shows up as a filament jump /
                # grind right after the pause.
                self._rebase_extruder_position(extruder, pos)
        t4 = reactor.monotonic()
        self._set_kinematic_position(self.toolhead_pos)
        t5 = reactor.monotonic()
        self._log_e_state("frozen")
        logging.info("quick pause timing: flush=%.0fms stop=%.0fms "
                     "query_ext=%.0fms rebase=%.0fms setpos=%.0fms",
                     (t1 - t0) * 1000., (t2 - t1) * 1000.,
                     (t3 - t2) * 1000., (t4 - t3) * 1000.,
                     (t5 - t4) * 1000.)

    def _rebase_extruder_position(self, extruder, e_pos):
        # Re-base the extruder's own motion state on the frozen E position.
        toolhead = self.printer.lookup_object('toolhead')
        ffi_main, ffi_lib = chelper.get_ffi()
        # E movement lives in the trapq x slot (see PrinterExtruder.move)
        ffi_lib.trapq_set_position(extruder.get_trapq(), toolhead.print_time,
                                   e_pos, 0., 0.)
        extruder.last_position = e_pos
        logging.info("quick pause: extruder %s re-based to E=%.4f",
                     extruder.get_name(), e_pos)

    def _line_bytes(self, line):
        if sys.version_info.major >= 3:
            return len(line.encode('utf-8'))
        return len(line)

    def _note_line_dispatch(self, line_no, line_start):
        # Maintain a sparse index of line number -> byte offset of the line start
        # while lines are dispatched.  Without it a pause would have to read the
        # file from the beginning to find the resume offset, which on a 1GB print
        # file takes seconds.
        index = self._line_index
        if index and line_no <= index[-1][0]:
            # The file was repositioned backwards (resume after pause, M26, ...):
            # drop every entry at or after this line, they are stale now.
            del index[bisect.bisect_left(index, (line_no, 0)):]
        if not index or line_no - index[-1][0] >= self._line_index_stride:
            index.append((line_no, line_start))

    def _lookup_position_by_line(self, target_line):
        # Fast path: take the nearest checkpoint at or before target_line and walk
        # forward at most one stride.  Returns None when the index can not serve
        # this target, so the caller falls back to a full scan.
        index = self._line_index
        if not index:
            return None
        i = bisect.bisect_right(index, (target_line, float('inf'))) - 1
        if i < 0:
            return None
        line_no, position = index[i]
        if line_no > target_line:
            return None
        if target_line - line_no > self._line_index_stride * 2:
            return None
        self.current_file.seek(position)
        while line_no < target_line:
            line = self.current_file.readline()
            if not line:
                logging.warning("virtual_sdcard: line %d is past end of file",
                                target_line)
                return None
            position += self._line_bytes(line)
            line_no += 1
        return position

    def _scan_position_by_line(self, target_line):
        # Slow fallback: scan the file from the beginning.
        self.current_file.seek(0)
        current_line = 0
        position = 0
        while True:
            line = self.current_file.readline()
            if not line:
                logging.warning("virtual_sdcard: line %d is past end of file",
                                target_line)
                return None
            if current_line == target_line - 1:
                logging.info("Starting position of target line %d: %d",
                             target_line, position)
                return position
            position += self._line_bytes(line)
            current_line += 1

    def _get_position_by_line(self, target_line, quiet=False):
        # Byte offset of the start of target_line (1 based), or None when the
        # line is past the end of the file.
        if target_line <= 0:
            return 0
        try:
            position = self._lookup_position_by_line(target_line)
        except Exception:
            logging.exception("virtual_sdcard: line index lookup failed")
            position = None
        if position is None:
            position = self._scan_position_by_line(target_line)
        if position is not None and not quiet:
            logging.info("Starting position of target line %d: %d",
                         target_line, position)
        return position

    def _set_kinematic_position(self, pos):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.get_last_move_time()
        if len(pos) < 4:
            curpos = toolhead.get_position()
            pos = [pos[0], pos[1], pos[2], curpos[3]]
        # Only claim the axes that were already homed.  The position read back
        # from the MCUs is real, but an axis that was never homed must not be
        # marked as homed - that would enable its range limits and let moves on
        # it run without a homing check.
        kin_status = toolhead.get_status(self.printer.get_reactor().monotonic())
        homing_axes = "".join([a for a in "xyz" if a in kin_status['homed_axes']])
        logging.info("set kinematic position pos=%.3f,%.3f,%.3f,%.3f homed=%s",
                     pos[0], pos[1], pos[2], pos[3], homing_axes or '-')
        toolhead.set_position(pos, homing_axes=homing_axes)

    def cmd_GET_TASKLINE(self, gcmd):
        if not self.must_pause_work:
            self._get_runline()
        formatted_pairs = [f"{key}:{value}" for key, value in self.linfo]
        formatted_line = " ".join(formatted_pairs)
        gcmd.respond_info(f"stepper line: {formatted_line}")
    # ---- power loss: retract while the PSU capacitors still hold ----------
    PLR_TIMING_LOG = "/opt/printer_data/logs/power_loss.log"
    def _power_loss_note(self, msg):
        # fsync'ed on purpose: the last line has to survive the power cut, it is
        # what tells us how much of the capacitor hold-up time was used.
        try:
            with open(self.PLR_TIMING_LOG, "a") as fh:
                fh.write("%.3f %s\n" % (self.reactor.monotonic(), msg))
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            logging.exception("power loss: cannot write the timing log")
    cmd_POWER_LOSS_RETRACT_help = (
        "Freeze the queued motion and retract the filament immediately after a "
        "mains failure (called by Moonraker's power_outage button)")
    cmd_POWER_LOSS_MARK_help = (
        "Write a fsync'ed marker to power_loss.log (used to measure how long "
        "the PSU capacitors kept the host alive)")
    def cmd_POWER_LOSS_MARK(self, gcmd):
        self._power_loss_note(gcmd.get('MSG', 'mark'))
    def cmd_POWER_LOSS_RETRACT(self, gcmd):
        retract = gcmd.get_float('RETRACT', 8.0, minval=0.)
        speed = gcmd.get_float('SPEED', 60.0, above=0.)
        hop = gcmd.get_float('HOP', 0.0, minval=0.)
        hop_speed = gcmd.get_float('HOP_SPEED', 10.0, above=0.)
        escape = gcmd.get_float('ESCAPE', 15.0, minval=0.)
        escape_speed = gcmd.get_float('ESCAPE_SPEED', 200.0, above=0.)
        escape_max_ms = gcmd.get_int('ESCAPE_MAX_MS', 700, minval=0, maxval=2000)
        anchor = gcmd.get_int('ANCHOR', 60, minval=0, maxval=500)
        t0 = self.reactor.monotonic()
        self._power_loss_note(
            "event: retract=%.1fmm speed=%.0fmm/s hop=%.2fmm anchor=%dms"
            % (retract, speed, hop, anchor))
        try:
            toolhead = self.printer.lookup_object('toolhead')
            if not self.is_active():
                self._power_loss_note("no active print, nothing to freeze")
                return
            # A print that was paused before the outage is already parked with
            # the pause retract (12 mm) in place; pulling another 8 mm here
            # would only have to be made up on resume.
            pause_resume = self.printer.lookup_object('pause_resume', None)
            if pause_resume is not None and pause_resume.is_paused:
                self._power_loss_note("print was paused (already retracted), "
                                      "skipping the outage retract")
                try:
                    self.gcode.run_script_from_command(
                        "SAVE_VARIABLE VARIABLE=plr_loss_done VALUE=0")
                except Exception:
                    logging.exception("power loss: cannot store plr_loss_done")
                return
            # Freeze exactly like the fast pause does: drop the pending steps,
            # rebase the toolhead on what the MCUs really reached and re-anchor
            # print_time to "mcu now + anchor" so the retract can start at once
            # instead of behind the ~2 s look-ahead.
            old_anchor = getattr(self, 'pause_anchor_ms', None)
            self.pause_anchor_ms = anchor
            try:
                self.do_pause(capture=False)
            finally:
                self.pause_anchor_ms = old_anchor
            t1 = self.reactor.monotonic()
            self._power_loss_note("frozen after %.0f ms" % ((t1 - t0) * 1000.))
            # _anchor_time_base() forces start_buffer_time >= 0.4 s (the pause
            # safety margin).  That is what delayed the 8 mm retract by ~0.4 s
            # on 2026-10-27.  In an outage the rails are about to die, so trade
            # that margin for an earlier retract.
            try:
                toolhead.start_buffer_time = min(
                    getattr(toolhead, 'start_buffer_time', 0.4), 0.10)
            except Exception:
                logging.exception("power loss: cannot lower start_buffer_time")
            if retract > 0.:
                # A leftover M220 override (e.g. from a make-up ramp that was
                # cancelled) would slow the retract down by the same factor.
                try:
                    self.gcode.run_script_from_command("M220 S100")
                except Exception:
                    logging.exception("power loss: cannot reset M220")
                self.gcode.run_script_from_command(
                    "M83\nG1 E-%.3f F%.0f" % (retract, speed * 60.))
                # Remember what was pulled back so the resume file can push
                # exactly that amount back.  The write overlaps with the motion
                # (the move is already queued) and is fsync'ed by SAVE_VARIABLE,
                # so it also survives the outage when the caps allow.
                try:
                    self.gcode.run_script_from_command(
                        "SAVE_VARIABLE VARIABLE=plr_loss_done VALUE=%.3f" % retract)
                except Exception:
                    logging.exception("power loss: cannot store plr_loss_done")
            if hop > 0.:
                self.gcode.run_script_from_command(
                    "G91\nG1 Z%.3f F%.0f\nG90" % (hop, hop_speed * 60.))
            # Retracting deeper does not remove the melt that stays inside the
            # nozzle (16 mm and 24 mm produced exactly the same 2 mm drop), so
            # the only way to keep that drop off the part is to stand somewhere
            # else while it falls.  The slicer emits EXCLUDE_OBJECT_DEFINE, so
            # the object polygon is available at runtime.
            if escape > 0.:
                try:
                    self._plr_escape_xy(toolhead, escape, escape_speed,
                                        escape_max_ms)
                except Exception:
                    logging.exception("power loss: escape move failed")
            t2 = self.reactor.monotonic()
            toolhead.wait_moves()
            t3 = self.reactor.monotonic()
            self._power_loss_note(
                "queued +%.0f ms, executed +%.0f ms (total %.0f ms)"
                % ((t2 - t0) * 1000., (t3 - t0) * 1000., (t3 - t0) * 1000.))
            # Did the drivers still have VMOT while the retract ran?  uv_cp (charge
            # pump undervoltage) is set when VS collapses, so this tells us whether
            # the steppers could physically follow the command.
            try:
                tmc = self.printer.lookup_object('tmc5160 extruder', None)
                if tmc is None:
                    self._power_loss_note("tmc extruder: object not found")
                else:
                    st = tmc.get_status(self.reactor.monotonic())
                    drv = st.get('drv_status') or {}
                    self._power_loss_note(
                        "tmc extruder: uv_cp=%s s2vsa=%s cs_actual=%s drv=%s"
                        % (drv.get('uv_cp'), drv.get('s2vsa'),
                           drv.get('cs_actual'), 'ok' if drv else 'none'))
            except Exception as exc:
                self._power_loss_note("tmc status read failed: %r" % (exc,))
        except Exception:
            self._power_loss_note(
                "FAILED after %.0f ms" % ((self.reactor.monotonic() - t0) * 1000.))
            logging.exception("power loss retract failed")
    def _snap_target(self, x, y, mm, park_x, park_y):
        """Where the fast tear should go.

        Returns (tx, ty, how) where how is 'polygon', 'park' or 'none'.
        Direction = towards the nearest side of the object bounding box, at most
        mm away (stops 1 mm outside the box).  Without a polygon it falls back to
        the park direction, which is the previous behaviour.
        """
        dx, dy = park_x - x, park_y - y
        d = (dx * dx + dy * dy) ** 0.5
        if d < 1e-6:
            return (x, y, 'none')
        tx, ty = x + dx / d * mm, y + dy / d * mm
        how = 'park'
        try:
            excl = self.printer.lookup_object('exclude_object', None)
            pts = []
            for obj in (getattr(excl, 'objects', None) or []):
                pts.extend(obj.get('polygon') or [])
            if pts:
                minx = min(p[0] for p in pts)
                maxx = max(p[0] for p in pts)
                miny = min(p[1] for p in pts)
                maxy = max(p[1] for p in pts)
                inside = (minx <= x <= maxx) and (miny <= y <= maxy)
                if inside:
                    # leave the part through the nearest side
                    sides = [((minx - 1.0) - x, 0.0),
                             ((maxx + 1.0) - x, 0.0),
                             (0.0, (miny - 1.0) - y),
                             (0.0, (maxy + 1.0) - y)]
                    bx, by = min(sides, key=lambda t: t[0] ** 2 + t[1] ** 2)
                else:
                    # already outside: keep going AWAY (never back towards the part)
                    bx = -1.0 if x < minx else (1.0 if x > maxx else 0.0)
                    by = -1.0 if y < miny else (1.0 if y > maxy else 0.0)
                    if bx != 0.0 and by != 0.0:
                        k = 0.7071067811865476
                        bx, by = bx * k, by * k
                bd = (bx * bx + by * by) ** 0.5
                if bd > 1e-6:
                    # normalise, then always travel the full mm: stopping at the
                    # side made the tear only ~1 mm long (no speed -> no tear)
                    tx, ty = x + (bx / bd) * mm, y + (by / bd) * mm
                    how = 'polygon'
        except Exception:
            logging.exception("quick pause: snap direction lookup failed")
        try:
            toolhead = self.printer.lookup_object('toolhead')
            st = toolhead.get_status(self.reactor.monotonic())
            axmin, axmax = st['axis_minimum'], st['axis_maximum']
            cx = min(max(tx, axmin[0] + 5.), axmax[0] - 5.)
            cy = min(max(ty, axmin[1] + 5.), axmax[1] - 5.)
            if ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5 < 0.4 * mm:
                # the object sits against the bed edge: keep the full-length tear
                # and go towards the park instead (it is off the part anyway)
                tx, ty = x + dx / d * mm, y + dy / d * mm
                tx = min(max(tx, axmin[0] + 5.), axmax[0] - 5.)
                ty = min(max(ty, axmin[1] + 5.), axmax[1] - 5.)
                how = 'park'
            else:
                tx, ty = cx, cy
        except Exception:
            logging.exception("quick pause: cannot clamp the snap target")
        return (tx, ty, how)

    # ---- repeating "resume in progress" toast ---------------------------
    # Plain reactor timer + gcode.respond_raw(): no g-code mutex involved, so it
    # keeps firing while a blocking M109 holds the queue.
    cmd_START_RESUME_TOAST_help = (
        "Start a repeating console toast (survives a blocking M109)")
    def cmd_START_RESUME_TOAST(self, gcmd):
        msg = gcmd.get('MSG', "Resume in progress - please do not operate the "
                              "printer until printing continues.")
        iv = gcmd.get_float('INTERVAL', 8.0, minval=1.0)
        self._resume_toast_stop()
        self._resume_toast_msg = msg
        self._resume_toast_iv = iv
        try:
            self.gcode.respond_raw("echo: " + msg)
        except Exception:
            logging.exception("resume toast: first message failed")
        try:
            self._resume_toast_timer = self.reactor.register_timer(
                self._resume_toast_tick, self.reactor.monotonic() + iv)
        except Exception:
            logging.exception("resume toast: cannot start the timer")

    def _resume_toast_tick(self, eventtime):
        try:
            self.gcode.respond_raw("echo: " + self._resume_toast_msg)
        except Exception:
            logging.exception("resume toast: repeat failed")
        return eventtime + self._resume_toast_iv

    def _resume_toast_stop(self):
        t = getattr(self, '_resume_toast_timer', None)
        if t is not None:
            try:
                self.reactor.update_timer(t, self.reactor.NEVER)
            except Exception:
                pass
            self._resume_toast_timer = None

    cmd_STOP_RESUME_TOAST_help = "Stop the repeating resume toast"
    def cmd_STOP_RESUME_TOAST(self, gcmd):
        self._resume_toast_stop()

    cmd_QUICK_PAUSE_SNAP_help = (
        "Quick pause: tear the ooze thread with a fast move AWAY from the printed "
        "object (uses the slicer's object polygon), with an optional retract")
    def cmd_QUICK_PAUSE_SNAP(self, gcmd):
        mm = gcmd.get_float('MM', 12.0, minval=0.)
        speed = gcmd.get_float('SPEED', 300.0, above=0.)
        retract = gcmd.get_float('RETRACT', 0.0, minval=0.)
        park_x = gcmd.get_float('PX', 45.0)
        park_y = gcmd.get_float('PY', 20.0)
        if mm <= 0.:
            return
        toolhead = self.printer.lookup_object('toolhead')
        pos = toolhead.get_position()
        x, y = pos[0], pos[1]
        tx, ty, how = self._snap_target(x, y, mm, park_x, park_y)
        if how == 'none':
            logging.info("quick pause: snap skipped (already at the park)")
            return
        logging.info("quick pause: snap %s (%.2f,%.2f) -> (%.2f,%.2f) "
                     "dist=%.2f mm retract=%.2f speed=%.0f",
                     how, x, y, tx, ty,
                     ((tx - x) ** 2 + (ty - y) ** 2) ** 0.5, retract, speed)
        try:
            self.gcode.run_script_from_command(
                "M83\nG1 X%.3f Y%.3f E-%.3f F%.0f"
                % (tx, ty, retract, speed * 60.))
        except Exception:
            logging.exception("quick pause: snap move failed")

    def _plr_escape_xy(self, toolhead, margin, speed, max_ms):
        """Get off the printed object, within the capacitor hold-up budget.

        The distance to the nearest polygon edge is at most half the object's
        diagonal, so even a full-bed print is reachable - but only if the move
        is fast and the budget is spent on the travel instead of on waiting.
        """
        excl = self.printer.lookup_object('exclude_object', None)
        objs = getattr(excl, 'objects', None) or []
        pts = []
        for obj in objs:
            pts.extend(obj.get('polygon') or [])
        if not pts:
            self._power_loss_note("escape: no object polygon available, "
                                  "staying on the part")
            return
        minx = min(p[0] for p in pts)
        maxx = max(p[0] for p in pts)
        miny = min(p[1] for p in pts)
        maxy = max(p[1] for p in pts)
        pos = toolhead.get_position()
        x, y = pos[0], pos[1]
        st = toolhead.get_status(self.reactor.monotonic())
        axmin, axmax = st['axis_minimum'], st['axis_maximum']
        targets = [(minx - margin, y), (maxx + margin, y),
                   (x, miny - margin), (x, maxy + margin)]
        best = None
        for tx, ty in targets:
            tx = min(max(tx, axmin[0] + 5.), axmax[0] - 5.)
            ty = min(max(ty, axmin[1] + 5.), axmax[1] - 5.)
            dist = ((tx - x) ** 2 + (ty - y) ** 2) ** 0.5
            if best is None or dist < best[0]:
                best = (dist, tx, ty)
        dist, tx, ty = best
        if dist < 1.:
            self._power_loss_note("escape: already clear of the object")
            return
        budget = speed * max(0., max_ms) / 1000.
        clipped = False
        if budget > 0. and dist > budget:
            ux, uy = (tx - x) / dist, (ty - y) / dist
            tx, ty = x + ux * budget, y + uy * budget
            dist = budget
            clipped = True
        # Raise the velocity limit for this one move: the pause default is
        # 300 mm/s and the budget leaves no room for a slow travel.
        self.gcode.run_script_from_command(
            "SET_VELOCITY_LIMIT VELOCITY=%.0f ACCEL=8000\nG90\nG1 X%.3f Y%.3f "
            "F%.0f" % (max(speed, 10.), tx, ty, speed * 60.))
        self._power_loss_note(
            "escape: (%.1f,%.1f) -> (%.1f,%.1f), %.1f mm%s, bbox=[%.1f..%.1f, "
            "%.1f..%.1f], %.0f mm/s"
            % (x, y, tx, ty, dist, ' (clipped by budget)' if clipped else '',
               minx, maxx, miny, maxy, speed))
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
        # Arbitrary byte repositioning invalidates the line index
        self._line_index = []
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
        cancel_on_error = False
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
            self._note_line_dispatch(self.file_line, self.file_position)
            if sys.version_info.major >= 3:
                next_file_position = self.file_position + len(line.encode()) + 1
            else:
                next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            try:
                self.gcode.run_script(line)
            except self.gcode.error as e:
                error_message = str(e)
                cancel_on_error = self.gcode.is_cancel_requested()
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
            if cancel_on_error:
                self.print_stats.note_cancel()
            else:
                self.print_stats.note_error(error_message)
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
        return self.reactor.NEVER

def load_config(config):
    return VirtualSD(config)

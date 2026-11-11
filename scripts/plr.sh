#!/bin/bash
# The three paths can be overridden so the script can be exercised in a sandbox
# (see /tmp/plrtest) without touching the live printer data.
PLR_DIR="${PLR_DIR_OVERRIDE:-/opt/printer_data/gcodes/.plr}"
CONFIG_FILE="${PLR_CONFIG_OVERRIDE:-/opt/printer_data/config/config_variables.cfg}"
LOG_FILE="${PLR_LOG_OVERRIDE:-/opt/printer_data/logs/moonraker.log}"
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> [$(date '+%F %T')] <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<" >> "$LOG_FILE"

z_offset="$1"
# _RESUME_INTERRUPTED passes the lift that _PLR_PARK already performed (mm) and
# the Z hop the outage handler did before the escape move (mm).  Both are part
# of the physical height that _PLR_PARK re-declares as Z=0, so both have to be
# added to the Z declaration below - forgetting the hop left the resumed print
# exactly hop mm too high (2026-10-28, stringy "printing in the air").
prelift="${2:-10}"
hop="${3:-0}"

mkdir -p "$PLR_DIR" || {
    echo "[$(date '+%F %T')] ERROR: Unable to create the directory: $PLR_DIR" >> "$LOG_FILE"
    exit 2
}

filepath=$(sed -n "s/.*filepath *= *'\([^']*\)'.*/\1/p" "$CONFIG_FILE")
if [[ -z "$filepath" || ! -f "$filepath" ]]; then
    echo "ERROR: filepath is empty or file does not exist: '$filepath'" >> "$LOG_FILE"
    exit 1
fi
cp "${filepath}" "/opt/plrtmpA.$$" || {
    echo "ERROR: Unable to copy file: $filepath" >> "$LOG_FILE"
    exit 1
}
sourcefile="/opt/plrtmpA.$$"
trap 'rm -f "$sourcefile"' EXIT

raw_line=$(sed -n -E "s/power_resume_line[[:space:]]*=[[:space:]]*([1-9][0-9]*)['\"]?[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE")
run_line=$(tr -cd '0-9' <<< "$raw_line")
if [[ ! "$run_line" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: power_resume_line, It must be a valid positive integer" >> "$LOG_FILE"
    echo "Current configuration value:'$run_line'" >> "$LOG_FILE"
    exit 1
fi
run_line=$((run_line - 1))
echo "When power outage, the printer is executing $run_line line of the file." >> "$LOG_FILE"

last_file="${filepath##*/}"

echo "Power-off recovery file path: $filepath" >> "$LOG_FILE"
echo "Last printed file name: $last_file" >> "$LOG_FILE"
plr="$last_file"

awk '
    {
        sub(/\r$/, "")
        if ($0 ~ /^;/ || $0 ~ /^[[:space:]]*$/) {
            print
        } else {
            exit
        }
    }' "$sourcefile" > "${PLR_DIR}/${plr}"

line="$run_line"

z_height=$(head -n "$line" "$sourcefile" | tac | awk '
  /^[[:space:]]*G[01][[:space:]].*[zZ]/ {
    if (match($0, /[zZ][[:space:]]*[-+]?[0-9]*\.?[0-9]+/)) {
      z_str = substr($0, RSTART, RLENGTH)
      if (match(z_str, /[-+]?[0-9]*\.?[0-9]+/)) {
        print substr(z_str, RSTART, RLENGTH)
        exit
      }
    }
  }
')
if [[ -z "$z_height" ]]; then
    echo "ERROR: Unable to determine Z height at line $line" >> "$LOG_FILE"
    exit 1
fi

z_ahead=$(sed -n "${line},$((line+3))p" "$sourcefile" | awk '
  /^[[:space:]]*G[01][[:space:]].*[zZ]/ {
    if (match($0, /[zZ][[:space:]]*[-+]?[0-9]*\.?[0-9]+/)) {
      z_str = substr($0, RSTART, RLENGTH)
      if (match(z_str, /[-+]?[0-9]*\.?[0-9]+/)) {
        v = substr(z_str, RSTART, RLENGTH)
        if (min == "" || v + 0 < min + 0) min = v
      }
    }
  }
  END { if (min != "") print min }
')
if [[ -n "$z_ahead" ]]; then
    z_height=$(awk -v h="$z_height" -v a="$z_ahead" 'BEGIN { print (a + 0 < h + 0) ? a : h }')
fi

echo "When power outage, the printing height: $z_height, the offset value: $z_offset" >> "$LOG_FILE"

# ---------------------------------------------------------------------------
# quick-pause: prefer the heights recorded by the PAUSE macro.
#
# The heuristic below assumes the park leaves the platform exactly
# custom_park_dz (20 mm) above the print plane, which is what the stock park
# does.  The quick-pause park ends on the nozzle cleaner at pause_park_z
# (~3.5 mm), so "print_z + 20" overshoots by ~18 mm and the final
# "G1 Z<print_z>" drives the platform 18 mm too far down.
# The pause macro records the two exact values instead:
#   power_park_z   = absolute Z the park finished on  -> what the platform is
#                    really at when power is lost while paused
#   power_resume_z = absolute Z the print was frozen at (correct even when the
#                    pause happened in the middle of a bed move, where the
#                    gcode text scan above only sees the move's target)
# Only used when the machine really was paused and both values look sane;
# otherwise the original heuristic is kept untouched.
# ---------------------------------------------------------------------------
is_paused=$(sed -n -E "s/^[[:space:]]*power_loss_paused[[:space:]]*=[[:space:]]*(True|False).*$/\1/p" "$CONFIG_FILE" | tail -n1)
rec_park_z=$(sed -n -E "s/^[[:space:]]*power_park_z[[:space:]]*=[[:space:]]*([-+]?[0-9]*\.?[0-9]+)[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE" | tail -n1)
rec_resume_z=$(sed -n -E "s/^[[:space:]]*power_resume_z[[:space:]]*=[[:space:]]*([-+]?[0-9]*\.?[0-9]+)[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE" | tail -n1)

z_target="$z_height"
if [ "$is_paused" = "True" ] && [ -n "$rec_resume_z" ] && \
   awk -v r="$rec_resume_z" 'BEGIN{exit !(r>0)}'; then
    # Sanity: the recorded frozen Z has to be close to the height the gcode text
    # reports for the resume line.  The text is only a plausibility reference,
    # never the value itself - a big mismatch means the record is stale (from an
    # earlier pause / an earlier print) and must not be trusted.
    plausible=True
    if [ -n "$z_height" ]; then
        plausible=$(awk -v r="$rec_resume_z" -v h="$z_height" \
            'BEGIN{ d = r - h; if (d < 0) d = -d; print (d <= 5) ? "True" : "False" }')
    fi
    if [ "$plausible" = "True" ] && [ -n "$rec_park_z" ] && \
       awk -v p="$rec_park_z" 'BEGIN{exit !(p>0)}'; then
        # both heights known and consistent: declare the true parked Z and come
        # back to the frozen print Z
        z_pos="$rec_park_z"
        z_target="$rec_resume_z"
        echo "quick-pause: exact recovery park_z=$z_pos resume_z=$z_target (paused)" >> "$LOG_FILE"
    else
        # parked height unknown (power lost while the park was still moving, so
        # _PAUSE_PARK_DONE had not written it yet) or the record is stale.
        # Declare the LOWEST plausible height and do not move:
        #   declared too low  -> the platform ends up higher than the print
        #                        plane: prints in the air, ugly but no crash
        #   declared too high -> the following absolute "G1 Z<print_z>" drives
        #                        the platform DOWN into the part
        if [ -n "$z_height" ]; then
            z_pos=$(awk -v r="$rec_resume_z" -v h="$z_height" \
                'BEGIN{ print (r + 0 < h + 0) ? r : h }')
        else
            z_pos="$rec_resume_z"
        fi
        z_target="$z_pos"
        echo "quick-pause: parked height unusable (park='$rec_park_z' plausible='$plausible'), declaring Z=$z_pos conservatively (no move)" >> "$LOG_FILE"
    fi
else
    z_pos=$(awk -v h="$z_height" -v o="$z_offset" 'BEGIN { print h + o }')
    echo "quick-pause: no recorded pause heights (paused='$is_paused' resume='$rec_resume_z'), falling back to print_z+offset" >> "$LOG_FILE"
fi
echo "Assign the Z height as: $z_pos" >> "$LOG_FILE"

# ---------------------------------------------------------------------------
# quick-pause PLR: _PLR_PARK (power_recovery.cfg) already lifted the head by
# <prelift> mm, homed XY and parked it BEFORE this file starts printing, so the
# platform physically sits prelift mm above the height recorded below.
# Declare that height, otherwise every following absolute Z move would dive
# prelift mm too deep.
# ---------------------------------------------------------------------------
z_declared=$(awk -v z="$z_pos" -v l="$prelift" -v h="$hop" 'BEGIN { print z + l + h }')
echo "quick-pause PLR: _PLR_PARK prelift=${prelift} + outage hop=${hop}, declaring Z=${z_declared} (recorded park_z=${z_pos}, resume_z=${z_target})" >> "$LOG_FILE"
echo "SET_KINEMATIC_POSITION Z=${z_declared}" >> "${PLR_DIR}/${plr}"
echo ";Z:${z_declared}" >> "${PLR_DIR}/${plr}"
# Start from a known override state: a previous attempt (or a manual tweak) may
# have left M220/M221 somewhere else; _PLR_RAMP then applies its own stages.
echo "M220 S100" >> "${PLR_DIR}/${plr}"
echo "M221 S100" >> "${PLR_DIR}/${plr}"

awk -v end_line="$line" '
    NR > end_line { exit }
    /M104|M140|M109|M190/ { print }
' "$sourcefile" >> "${PLR_DIR}/${plr}"

start_info=$(awk -v max_line="$line" '
    NR > max_line { exit }
    $0 ~ /^(START_PRINT|PRINT_START)/ {
        print
        exit
    }
' "$sourcefile")
if [ -n "$start_info" ]; then
    EXTRUDER=$(echo "$start_info" | sed -n 's/.*EXTRUDER=\([0-9]*\).*/\1/p')
    EXTRUDER1=$(echo "$start_info" | sed -n 's/.*EXTRUDER1=\([0-9]*\).*/\1/p')
    BED=$(echo "$start_info" | sed -n 's/.*BED=\([0-9]*\).*/\1/p')
    CHAMBER=$(echo "$start_info" | sed -n 's/.*CHAMBER=\([0-9]*\).*/\1/p')
    EXTRUDER=${EXTRUDER:-0}
    EXTRUDER1=${EXTRUDER1:-0}
    BED=${BED:-0}
    CHAMBER=${CHAMBER:-0}

    temp_cmds=("M140 S" "M104 T0 S" "M104 T1 S" "M141 S" "M190 S" "M109 T0 S" "M109 T1 S" "M191 S")
    temps=("$BED" "$EXTRUDER" "$EXTRUDER1" "$CHAMBER" "$BED" "$EXTRUDER" "$EXTRUDER1" "$CHAMBER")

    for i in "${!temps[@]}"; do
        if [ "${temps[$i]}" != "0" ]; then
            echo "${temp_cmds[$i]} ${temps[$i]}" >> "${PLR_DIR}/${plr}"
        fi
    done
fi


if [[ "$is_plr_file" == "False" ]]; then
awk -v plr="${PLR_DIR}/${plr}" '
    BEGIN {
        bed_temp = 0
        print_temp = 0
        print_temp1 = 0
    }
    {
        gsub(/\r/, "")
        # 兼容 material_bed_temperature 和 first_layer_bed_temperature
        if (match($0, /(material_bed_temperature|first_layer_bed_temperature)[ =]+([0-9]+)/, m))
            bed_temp = m[2]
        # 兼容 material_print_temperature 和 nozzle_temperature
        if (match($0, /(material_print_temperature|nozzle_temperature_initial_layer|nozzle_temperature)[ =]+([0-9]+)/, m)) {
            if (print_temp == 0) print_temp = m[2]
            else if (print_temp1 == 0) print_temp1 = m[2]
        }
    }
    END {
        if (bed_temp > 0 || print_temp > 0) {
            print "; 温度控制指令"
            if (bed_temp > 0) {
                printf "M140 S%d\nM190 S%d\n", bed_temp, bed_temp
            }
            if (print_temp > 0) {
                printf "M104 T0 S%d\nM109 T0 S%d\n", print_temp, print_temp
            }
            if (print_temp1 > 0) {
                printf "M104 T1 S%d\nM109 T1 S%d\n", print_temp1, print_temp1
            }
        }
    }
' "$sourcefile" >> "${PLR_DIR}/${plr}"
fi

# ---------------------------------------------------------------------------
# quick-pause PLR: the park is done by _PLR_PARK before the file is printed, so
# the preamble only has to restore the copy mode / active tool, walk back to the
# recorded point, prime there and start the slow->fast make-up ramp.
# ---------------------------------------------------------------------------
{
    awk -v end_line="$line" '
        NR > end_line { exit }
        /ACTIVATE_COPY_MODE|ACTIVATE_MIRROR_MODE/ { print }
    ' "$sourcefile"
    awk -v end_line="$line" '
        NR > end_line { exit }
        /^T[01]/ { last_match = $0; found = 1 }
        END { if (found) print last_match }
    ' "$sourcefile"
} >> "${PLR_DIR}/${plr}"

# Last absolute X/Y at or before the resume line: that is where the print was.
xy_target=$(awk -v end_line="$line" '
    NR > end_line { exit }
    {
        x = ""; y = ""
        if (match($0, /X[-+]?[0-9]*\.?[0-9]+/))
            x = substr($0, RSTART + 1, RLENGTH - 1)
        if (match($0, /Y[-+]?[0-9]*\.?[0-9]+/))
            y = substr($0, RSTART + 1, RLENGTH - 1)
        if (x != "" && y != "") { last_x = x; last_y = y }
    }
    END { if (last_x != "") print last_x, last_y }
' "$sourcefile")

# Feedrate the print was using when the power was lost.  The resumed lines
# usually carry no F of their own, and _PLR_RESUME_MOVE leaves the g-code
# feedrate at the slow landing speed (F60).  Without restoring it the print
# crawls at 0.2 mm/s once M220 applies the 20 % make-up ramp - the SD queue then
# backs up for minutes, pause/cancel appear dead and nothing seems to move
# (reported on the 2026-10-27 test).
last_feed=$(awk -v end_line="$line" '
    NR > end_line { exit }
    {
        if (match($0, /F[0-9]*\.?[0-9]+/))
            f = substr($0, RSTART + 1, RLENGTH - 1)
    }
    END { if (f != "") print f }
' "$sourcefile")

if [ -n "$xy_target" ]; then
    x_target="${xy_target%% *}"
    y_target="${xy_target##* }"
    {
        echo "SET_GCODE_VARIABLE MACRO=RESUME VARIABLE=resume_x VALUE=${x_target}"
        echo "SET_GCODE_VARIABLE MACRO=RESUME VARIABLE=resume_y VALUE=${y_target}"
        echo "SET_GCODE_VARIABLE MACRO=RESUME VARIABLE=resume_z VALUE=${z_target}"
        echo "_PLR_RESUME_MOVE"
    } >> "${PLR_DIR}/${plr}"
    echo "quick-pause PLR: walk back to X=${x_target} Y=${y_target} Z=${z_target}" >> "$LOG_FILE"
else
    # No absolute X/Y found: never travel sideways from the park, only descend.
    echo "G1 Z${z_target} F3000" >> "${PLR_DIR}/${plr}"
    echo "quick-pause PLR: no XY target found, descending at the park" >> "$LOG_FILE"
fi

if [ -n "$last_feed" ]; then
    echo "G1 F${last_feed}" >> "${PLR_DIR}/${plr}"
    echo "quick-pause PLR: restore feedrate F${last_feed}" >> "$LOG_FILE"
else
    # No F anywhere before the resume line: fall back to a conservative print
    # speed instead of inheriting the 1 mm/s landing feedrate.
    echo "G1 F1800" >> "${PLR_DIR}/${plr}"
    echo "quick-pause PLR: no F found before the resume line, defaulting to F1800" >> "$LOG_FILE"
fi

echo "_PLR_RAMP" >> "${PLR_DIR}/${plr}"
echo "M106 S204" >> "${PLR_DIR}/${plr}"

# ---------------------------------------------------------------------------
# E reference for the resumed lines.  Emitted AFTER the prime so that the
# make-up filament does not shift the absolute/relative E accounting.
# ---------------------------------------------------------------------------
awk -v end_line="$line" '
    BEGIN { last_mode = ""; e_value = "" }
    # 跳过注释行
    $0 ~ /^;/ { next }
    # 只处理到end_line行
    NR > end_line { exit }
    # 记录最后出现的模式
    /M83/ { last_mode = "M83" }
    /M82/ { last_mode = "M82" }
    # 捕获end_line行的E值
    NR == end_line {
        if (match($0, /E[0-9.]+/)) {
            e_value = substr($0, RSTART+1, RLENGTH-1)
        }
    }
    # 最终决策
    END {
        if (last_mode == "M83") {
            print "G92 E0"
            print "M83"
        }
        else if (last_mode == "M82") {
            print "M82"
            print (e_value != "") ? "G92 E" e_value : "G92 E0"
        }
    }
' "$sourcefile" >> "${PLR_DIR}/${plr}"

sed -n "${line},$ p" "${sourcefile}" >> "${PLR_DIR}/${plr}"
echo "Append the file from ${line} line to ${PLR_DIR}/${plr}" >> "$LOG_FILE"

echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> [$(date '+%F %T')] <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<" >> "$LOG_FILE"

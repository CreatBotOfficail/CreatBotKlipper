#!/bin/bash
PLR_DIR="/home/klipper/printer_data/gcodes/.plr"
CONFIG_FILE="/home/klipper/printer_data/config/config_variables.cfg"
LOG_FILE="/home/klipper/printer_data/logs/moonraker.log"
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> [$(date '+%F %T')] <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<" >> "$LOG_FILE"

z_offset="$1"

mkdir -p "$PLR_DIR" || {
    echo "[$(date '+%F %T')] ERROR: Unable to create the directory: $PLR_DIR" >> "$LOG_FILE"
    exit 2
}

filepath=$(sed -n "s/.*filepath *= *'\([^']*\)'.*/\1/p" "$CONFIG_FILE")
cp "${filepath}" "/home/klipper/plrtmpA.$$"
sourcefile="/home/klipper/plrtmpA.$$"

raw_value=$(sed -n -E "s/^[[:space:]]*power_loss_paused[[:space:]]*=[[:space:]]*(True|False)[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE" | tail -n1)
is_pause=False
[[ "${raw_value,,}" == "true" ]] && is_pause=True

raw_line=$(sed -n -E "s/power_resume_line[[:space:]]*=[[:space:]]*([1-9][0-9]*)['\"]?[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE")
run_line=$(tr -cd '0-9' <<< "$raw_line")
if [[ ! "$run_line" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: power_resume_line, It must be a valid positive integer" >> "$LOG_FILE"
    echo "Current configuration value:'$run_line'" >> "$LOG_FILE"
    exit 1
fi
echo "When power outage, the printer is executing $run_line line of the file." >> "$LOG_FILE"

raw_position=$(sed -n -E "s/power_resume_position[[:space:]]*=[[:space:]]*([1-9][0-9]*)['\"]?[[:space:]]*(;.*)?$/\1/p" "$CONFIG_FILE")
target_pos=$(tr -cd '0-9' <<< "$raw_position")
if [[ ! "$target_pos" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: power_resume_position, It must be a valid positive integer" >> "$LOG_FILE"
    echo "Current configuration value:'$target_pos'" >> "$LOG_FILE"
    exit 1
fi

last_file="${filepath##*/}"

echo "Power-off recovery file path: $filepath" >> "$LOG_FILE"
echo "Last printed file name: $last_file" >> "$LOG_FILE"
plr="$last_file"

cat "${sourcefile}" | awk '
    {
        sub(/\r$/, "")
        if ($0 ~ /^;/ || $0 ~ /^[[:space:]]*$/) {
            print
        } else {
            exit
        }
    }' > "${PLR_DIR}/${plr}"

line=$(awk -v target="$target_pos" '
    BEGIN { current_pos = 0; line_number = 1 }
    {
        line_length = length($0) + 1  # +1 对应换行符(Linux: \n, Windows: \r\n)
        if (current_pos <= target && target < current_pos + line_length) {
            print line_number
            exit
        }
        current_pos += line_length
        line_number++
    }
' "$sourcefile")
echo "When power outage, it was parsing the $target_pos byte and the $line line" >> "$LOG_FILE"

line="$run_line"

z_height=$(awk -v target_line="$line" '
    BEGIN { found = 0 }
    NR <= target_line {
        if ($0 ~ /^;[[:space:]]*Z:/) {
            split($0, parts, /:[[:space:]]*/)
            z_val = parts[2]
            found = 1
        }
    }
    NR == target_line {
        if (found) {
            print z_val
        }
        exit
    }
' "$sourcefile")
echo "When power outage, the printing height: $z_height, the offset value: $z_offset" >> "$LOG_FILE"

z_pos=$(echo "${z_height} + ${z_offset}" | bc)
echo "Assign the Z height as: $z_pos" >> "$LOG_FILE"

echo "SET_KINEMATIC_POSITION Z=${z_pos}" >> "${PLR_DIR}/${plr}"
echo ";Z:${z_pos}" >> "${PLR_DIR}/${plr}"

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


awk -v plr="${PLR_DIR}/${plr}" '
    BEGIN {
        bed_temp = 0
        print_temp = 0
        cmd_generated = 0
    }
    $0 ~ /;End of Gcode/,0 {
        gsub(/\r?\n/, " ")
        gsub(/;/, "\n")
        gsub(/[ \t]+/, " ")

        if (match($0, /material_bed_temperature[ =]+([0-9]+)/, m))
            bed_temp = m[1]
        if (match($0, /material_print_temperature[ =]+([0-9]+)/, m))
            print_temp = m[1]

        if (bed_temp > 0 || print_temp > 0) {
            if (!cmd_generated) {
                print "; 温度控制指令"
                cmd_generated = 1
            }
            if (bed_temp > 0) {
                printf "M140 S%d\nM190 S%d\n", bed_temp, bed_temp
            }
            if (print_temp > 0) {
                printf "M104 S%d\nM109 S%d\n", print_temp, print_temp
            }
            exit
        }
    }
' "$sourcefile" >> "${PLR_DIR}/${plr}"

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

target_position=$(awk -v line_num="$line" 'NR == line_num { gsub(/E[0-9.]+/, ""); print }' "$sourcefile")
{
    echo 'G91'
    echo 'G1 Z10'
    echo 'G90'
    echo 'G28 X Y'
    awk -v end_line="$line" '
        NR > end_line { exit }
        /ACTIVATE_COPY_MODE|ACTIVATE_MIRROR_MODE/ { print }
    ' "$sourcefile"
    echo 'G1 X5'
    echo 'G1 Y5'
    awk -v end_line="$line" '
        NR > end_line {
            if (found) {
                print last_match
            }
            exit
        }
        /^T[01]/ {
            last_match = $0
            found = 1
        }
        END {
            if (found) {
                print last_match
            }
        }
    ' "$sourcefile"

    if [ -n "$target_position" ]; then
        echo "$target_position"
    fi
    echo 'M106 S204'
} >> "${PLR_DIR}/${plr}"

echo "G1 Z${z_height} F3000" >> "${PLR_DIR}/${plr}"

sed -n "${line},$ p" "${sourcefile}" >> "${PLR_DIR}/${plr}"
echo "Append the file from ${line} line to ${PLR_DIR}/${plr}" >> "$LOG_FILE"

rm /home/klipper/plrtmpA.$$
echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> [$(date '+%F %T')] <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<" >> "$LOG_FILE"

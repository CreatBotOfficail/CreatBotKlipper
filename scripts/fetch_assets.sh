#!/bin/bash

# Get model name and type
Type="$1"
Model="$2"
Filename="$3"

echo "Type: ${Type}"
echo "Model: ${Model}"
echo "Filename: ${Filename}"

# Check if model name is provided
if [ -z "$Model" ]; then
  echo "Error: Model name not provided"
  exit 1
fi

# Check if the Type is Gcode
if [ "$Type" = "Gcode" ]; then
    # Define download path and filename
    download_path="$HOME/.temp/"
    download_filename="${download_path}NozzleAglin.zip"
    download_url="https://www.creatbot.com/downloads/3dmodels/gcode/${Model}/NozzleAglin.zip"

    # Create temporary download directory
    mkdir -p "${download_path}"

    # Define target folder for extracted file
    target_folder="$HOME/printer_data/gcodes/.PresetModel/"

    # Check if the target folder already contains the extracted file
    if [ -f "${target_folder}NozzleAglin.gcode" ]; then
        echo "Target file 'NozzleAglin.gcode' already exists, skipping download and extraction."
        exit 0
    fi

    # If the file does not exist, start downloading the file
    wget -q -O "${download_filename}" "${download_url}"

    # Check if wget successfully downloaded the file
    if [ $? -ne 0 ]; then
        echo "Error: Download failed, the URL might be invalid or there is a network issue."
        exit 1
    fi

    # Create target extraction directory if it doesn't exist
    mkdir -p "$target_folder"

    # Extract the downloaded file to the target directory, using -o to overwrite existing files
    unzip -o -q "${download_filename}" -d "$target_folder"

    # Check if the extraction was successful
    if [ $? -ne 0 ]; then
        echo "Error: Extraction failed, please check if the file is corrupted or the path is correct."
        exit 1
    fi

    # Remove the temporary download folder
    rm -rf "${download_path}"

    echo "Download and extraction completed successfully."

fi

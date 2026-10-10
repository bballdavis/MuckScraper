#!/bin/bash

# Define the output file path
output_file="$HOME/aggregator.txt"

# Clear the output file if it exists
> "$output_file"

# Loop through each Python file in the aggregator folder and grep its contents
for file in aggregator/*.py; do
    # Check if the file exists to avoid errors
    if [ -f "$file" ]; then
        echo "Grep results for $file:" >> "$output_file"
        grep -Hn "" "$file" >> "$output_file"
        echo "" >> "$output_file"  # Add a newline for separation
    fi
done

echo "Grep results have been exported to $output_file"

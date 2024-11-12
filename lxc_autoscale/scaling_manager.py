from config import LXC_TIER_ASSOCIATIONS, config
import logging  # For logging events and errors
from datetime import datetime, timedelta  # For handling dates and times
from lxc_utils import (  # Import necessary utility functions related to LXC management
    run_command, get_containers, is_container_running, backup_container_settings,
    load_backup_settings, rollback_container_settings, log_json_event, get_total_cores,
    get_total_memory, get_cpu_usage, get_memory_usage, is_ignored, get_container_data,
    collect_container_data, prioritize_containers, get_container_config,
    generate_unique_snapshot_name, generate_cloned_hostname
)
from notification import send_notification  # Import the notification function
from config import HORIZONTAL_SCALING_GROUPS, IGNORE_LXC, DEFAULTS  # Import configuration constants
import paramiko

# Constants for repeated values
TIMEOUT_EXTENDED = 300  # Extended timeout for scaling operations
CPU_SCALE_DIVISOR = 10  # Divisor for dynamic CPU scaling
MEMORY_SCALE_FACTOR = 10  # Factor for dynamic memory scaling

# Dictionary to track the last scale-out action for each group
scale_last_action = {}

def generate_unique_snapshot_name(base_name):
    """
    Generate a unique name for a snapshot using the current timestamp.

    Args:
        base_name (str): The base name for the snapshot.

    Returns:
        str: A unique snapshot name.
    """
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    return f"{base_name}-{timestamp}"

def generate_cloned_hostname(base_name, clone_number):
    """
    Generate a hostname for a cloned container to ensure uniqueness.

    Args:
        base_name (str): The base name of the container.
        clone_number (int): The clone number to append.

    Returns:
        str: A unique hostname for the cloned container.
    """
    return f"{base_name}-cloned-{clone_number}"

def calculate_resource_delta(current, lower_threshold, upper_threshold, min_increment, max_increment):
    """
    Calculate the increment for the resource scaling. It's a simple lerp between min-max based on
    how far between 0-lower_threshold or upper_threshold-100 the current value is.
    """
    if current >= lower_threshold and current <= upper_threshold:
        return 0
    if current < lower_threshold:
        proportion = 1 - (current / lower_threshold)
    else:
        proportion = (current - upper_threshold) / (100-upper_threshold)
    assert(proportion >= 0)
    assert(proportion <= 1)
    increment_delta = max_increment-min_increment
    ratio_along_increment = increment_delta * proportion
    return round(min_increment + ratio_along_increment)

def calculate_increment(current, upper_threshold, min_increment, max_increment):
    """
    Calculate the increment for resource scaling based on current usage and thresholds.

    Args:
        current (float): Current usage percentage.
        upper_threshold (float): The upper threshold for usage.
        min_increment (int): Minimum increment value.
        max_increment (int): Maximum increment value.

    Returns:
        int: Calculated increment value.
    """
    proportional_increment = int((current - upper_threshold) / CPU_SCALE_DIVISOR)
    return min(max(min_increment, proportional_increment), max_increment)

def calculate_decrement(current, lower_threshold, current_allocated, min_decrement, min_allocated):
    """
    Calculate the decrement for resource scaling based on current usage and thresholds.

    Args:
        current (float): Current usage percentage.
        lower_threshold (float): The lower threshold for usage.
        current_allocated (int): Currently allocated resources (CPU or memory).
        min_decrement (int): Minimum decrement value.
        min_allocated (int): Minimum allowed allocated resources.

    Returns:
        int: Calculated decrement value.
    """
    dynamic_decrement = max(1, int((lower_threshold - current) / CPU_SCALE_DIVISOR))
    return max(min(current_allocated - min_allocated, dynamic_decrement), min_decrement)

def get_behaviour_multiplier():
    """
    Determine the behavior multiplier based on the current configuration.

    Returns:
        float: The behavior multiplier (1.0 for normal, 0.5 for conservative, 2.0 for aggressive).
    """
    if DEFAULTS['behaviour'] == 'conservative':
        return 0.5
    elif DEFAULTS['behaviour'] == 'aggressive':
        return 2.0
    return 1.0

def scale_memory(ctid, mem_usage, mem_upper, mem_lower, current_memory, min_memory, max_memory, available_memory, config):
    """
    Adjust memory for a container based on current usage.

    Args:
        ctid (str): Container ID.
        mem_usage (float): Current memory usage.
        mem_upper (float): Upper memory threshold.
        mem_lower (float): Lower memory threshold.
        current_memory (int): Currently allocated memory.
        min_memory (int): Minimum memory allowed.
        max_memory (int): Maximum memory allowed.
        available_memory (int): Available memory.
        config (dict): Configuration dictionary.

    Returns:
        tuple: Updated available memory and flag indicating if memory was changed.
    """
    memory_changed = False
    behaviour_multiplier = get_behaviour_multiplier()

    if mem_usage > mem_upper:
        increment = max(
            int(config['memory_min_increment'] * behaviour_multiplier),
            int((mem_usage - mem_upper) * config['memory_min_increment'] / MEMORY_SCALE_FACTOR)
        )
        if available_memory >= increment:
            logging.info(f"Increasing memory for container {ctid} by {increment}MB...")
            new_memory = current_memory + increment
            run_command(f"pct set {ctid} -memory {new_memory}")
            available_memory -= increment
            memory_changed = True
            log_json_event(ctid, "Increase Memory", f"{increment}MB")
            send_notification(f"Memory Increased for Container {ctid}", f"Memory increased by {increment}MB.")
        else:
            logging.warning(f"Not enough available memory to increase for container {ctid}")

    elif mem_usage < mem_lower and current_memory > min_memory:
        decrease_amount = calculate_decrement(
            mem_usage, mem_lower, current_memory,
            int(config['min_decrease_chunk'] * behaviour_multiplier), min_memory
        )
        if decrease_amount > 0:
            logging.info(f"Decreasing memory for container {ctid} by {decrease_amount}MB...")
            new_memory = min(current_memory - decrease_amount, max_memory)
            run_command(f"pct set {ctid} -memory {new_memory}")
            available_memory += decrease_amount
            memory_changed = True
            log_json_event(ctid, "Decrease Memory", f"{decrease_amount}MB")
            send_notification(f"Memory Decreased for Container {ctid}", f"Memory decreased by {decrease_amount}MB.")

    return available_memory, memory_changed

def adjust_resources(containers, energy_mode):
    """
    Adjust CPU and memory resources for each container based on usage.

    Args:
        containers (dict): A dictionary of container resource usage data.
        energy_mode (bool): Flag to indicate if energy-saving adjustments should be made during off-peak hours.
    """
    logging.info("Starting resource allocation process...")
    logging.info(f"Ignoring LXC Containers: {IGNORE_LXC}")

    total_cores = get_total_cores()
    total_memory = get_total_memory()

    reserved_cores = max(1, int(total_cores * DEFAULTS['reserve_cpu_percent'] / 100))
    reserved_memory = DEFAULTS['reserve_memory_mb']

    available_cores = total_cores - reserved_cores
    available_memory = total_memory - reserved_memory

    logging.debug(
        f"Total cores: {total_cores}, Reserved cores: {reserved_cores}, "
        f"Available cores: {available_cores}"
    )

    logging.debug(
        f"Total memory: {total_memory}MB, Reserved memory: {DEFAULTS['reserve_memory_mb']}MB, "
        f"Available memory: {available_memory}MB"
    )
    logging.info(f"Initial resources before adjustments: {available_cores} cores, {available_memory} MB memory")

    # Print current resource usage for all running LXC containers
    logging.info("Current resource usage for all containers:")
    for ctid, usage in containers.items():
        rounded_cpu_usage = round(usage['cpu'], 2)
        rounded_mem_usage = round(usage['mem'], 2)
        total_mem_allocated = usage['initial_memory']
        free_mem_percent = round(100 - rounded_mem_usage, 2)

        logging.info(f"Container {ctid}: CPU usage: {rounded_cpu_usage}%, Memory usage: {round(rounded_mem_usage*(1/100)*total_mem_allocated)}MB "
                     f"({free_mem_percent}% free of {total_mem_allocated}MB total), "
                     f"Initial cores: {usage['initial_cores']}, Initial memory: {total_mem_allocated}MB")

    # Proceed with the rest of the logic for adjusting resources
    for ctid, usage in containers.items():
        if ctid in IGNORE_LXC:
            logging.info(f"Container {ctid} is ignored. Skipping resource adjustment.")
            continue

        config = DEFAULTS | LXC_TIER_ASSOCIATIONS.get(ctid, DEFAULTS)
        cpu_upper = config['cpu_upper_threshold']
        cpu_lower = config['cpu_lower_threshold']
        mem_upper = config['memory_upper_threshold']
        mem_lower = config['memory_lower_threshold']
        min_cores = config['min_cores']
        max_cores = config['max_cores']
        min_memory = config['min_memory']
        max_memory = config['max_memory']

        cpu_usage = usage['cpu']
        mem_usage = usage['mem']

        current_cores = usage["initial_cores"]
        current_memory = usage["initial_memory"]

        cores_changed = False
        memory_changed = False

        behaviour_multiplier = get_behaviour_multiplier()

        # Adjust CPU cores if needed
        if cpu_usage > cpu_upper:
            increment = calculate_resource_delta(cpu_usage, cpu_lower, cpu_upper, config['core_min_increment'], config['core_max_increment'])
            new_cores = current_cores + increment

            logging.info(f"Container {ctid} - CPU usage exceeds upper threshold.")
            logging.info(f"Container {ctid} - Increment: {increment}, New cores: {new_cores}")

            if available_cores >= increment and new_cores <= max_cores:
                run_command(f"pct set {ctid} -cores {new_cores}")
                available_cores -= increment
                cores_changed = True
                log_json_event(ctid, "Increase Cores", f"{increment}")
                send_notification(f"CPU Increased for Container {ctid}", f"CPU cores increased to {new_cores}.")
            else:
                logging.warning(f"Container {ctid} - Not enough available cores to increase.")

        elif cpu_usage < cpu_lower and current_cores > min_cores:
            decrement = calculate_resource_delta(cpu_usage, cpu_lower, cpu_upper, config['core_min_increment'], config['core_max_increment'])
            new_cores = max(min_cores, current_cores - decrement)

            logging.info(f"Container {ctid} - CPU usage below lower threshold.")
            logging.info(f"Container {ctid} - Decrement: {decrement}, New cores: {new_cores}")

            if new_cores >= min_cores:
                run_command(f"pct set {ctid} -cores {new_cores}")
                available_cores += (current_cores - new_cores)
                cores_changed = True
                log_json_event(ctid, "Decrease Cores", f"{decrement}")
                send_notification(f"CPU Decreased for Container {ctid}", f"CPU cores decreased to {new_cores}.")
            else:
                logging.warning(f"Container {ctid} - Cannot decrease cores below min_cores.")

        # Adjust memory if needed
        available_memory, memory_changed = scale_memory(
            ctid, mem_usage, mem_upper, mem_lower, current_memory, min_memory, max_memory, available_memory, config
        )

        # Apply energy efficiency mode if enabled
        if energy_mode and is_off_peak():
            if current_cores > min_cores:
                logging.info(f"Reducing cores for energy efficiency during off-peak hours for container {ctid}...")
                run_command(f"pct set {ctid} -cores {min_cores}")
                available_cores += (current_cores - min_cores)
                log_json_event(ctid, "Reduce Cores (Off-Peak)", f"{current_cores - min_cores}")
                send_notification(f"CPU Reduced for Container {ctid}", f"CPU cores reduced to {min_cores} for energy efficiency.")
            if current_memory > min_memory:
                logging.info(f"Reducing memory for energy efficiency during off-peak hours for container {ctid}...")
                run_command(f"pct set {ctid} -memory {min_memory}")
                available_memory += (current_memory - min_memory)
                log_json_event(ctid, "Reduce Memory (Off-Peak)", f"{current_memory - min_memory}MB")
                send_notification(f"Memory Reduced for Container {ctid}", f"Memory reduced to {min_memory}MB for energy efficiency.")

    logging.info(f"Final resources after adjustments: {available_cores} cores, {available_memory} MB memory")


def manage_horizontal_scaling(containers):
    """
    Manage horizontal scaling based on CPU and memory usage across containers.

    Args:
        containers (dict): A dictionary of container resource usage data.
    """
    for group_name, group_config in HORIZONTAL_SCALING_GROUPS.items():
        current_time = datetime.now()
        last_action_time = scale_last_action.get(group_name, current_time - timedelta(hours=1))

        # Calculate average CPU and memory usage for the group
        total_cpu_usage = sum(containers[ctid]['cpu'] for ctid in group_config['lxc_containers'] if ctid in containers)
        total_mem_usage = sum(containers[ctid]['mem'] for ctid in group_config['lxc_containers'] if ctid in containers)
        num_containers = len(group_config['lxc_containers'])

        if num_containers > 0:
            avg_cpu_usage = total_cpu_usage / num_containers
            avg_mem_usage = total_mem_usage / num_containers
        else:
            avg_cpu_usage = 0
            avg_mem_usage = 0

        logging.debug(f"Group: {group_name} | Average CPU Usage: {avg_cpu_usage}% | Average Memory Usage: {avg_mem_usage}%")

        # Check if scaling out is needed based on usage thresholds
        if (avg_cpu_usage > group_config['horiz_cpu_upper_threshold'] or
            avg_mem_usage > group_config['horiz_memory_upper_threshold']):
            logging.debug(f"Thresholds exceeded for {group_name}. Evaluating scale-out conditions.")

            # Ensure enough time has passed since the last scaling action
            if current_time - last_action_time >= timedelta(seconds=group_config.get('scale_out_grace_period', 300)):
                scale_out(group_name, group_config)
        else:
            logging.debug(f"No scaling needed for {group_name}. Average usage below thresholds.")

def scale_out(group_name, group_config):
    """
    Scale out a horizontal scaling group by cloning a new container.

    Args:
        group_name (str): The name of the scaling group.
        group_config (dict): Configuration details for the scaling group.
    """
    current_instances = sorted(map(int, group_config['lxc_containers']))
    starting_clone_id = group_config['starting_clone_id']
    max_instances = group_config['max_instances']

    # Check if the maximum number of instances has been reached
    if len(current_instances) >= max_instances:
        logging.info(f"Max instances reached for {group_name}. No scale out performed.")
        return

    # Determine the next available clone ID
    new_ctid = starting_clone_id + len([ctid for ctid in current_instances if int(ctid) >= starting_clone_id])
    base_snapshot = group_config['base_snapshot_name']

    # Create a unique snapshot name
    unique_snapshot_name = generate_unique_snapshot_name("snap")

    logging.info(f"Creating snapshot {unique_snapshot_name} of container {base_snapshot}...")

    # Create the snapshot
    snapshot_cmd = f"pct snapshot {base_snapshot} {unique_snapshot_name} --description 'Auto snapshot for scaling'"
    if run_command(snapshot_cmd):
        logging.info(f"Snapshot {unique_snapshot_name} created successfully.")

        logging.info(f"Cloning container {base_snapshot} to create {new_ctid} using snapshot {unique_snapshot_name}...")

        # Clone the container using the snapshot, with an extended timeout
        clone_hostname = generate_cloned_hostname(base_snapshot, len(current_instances) + 1)
        clone_cmd = f"pct clone {base_snapshot} {new_ctid} --snapname {unique_snapshot_name} --hostname {clone_hostname}"
        if run_command(clone_cmd, timeout=TIMEOUT_EXTENDED):  # Extended timeout to 300 seconds
            # Network setup based on group configuration
            if group_config['clone_network_type'] == "dhcp":
                run_command(f"pct set {new_ctid} -net0 name=eth0,bridge=vmbr0,ip=dhcp")
            elif group_config['clone_network_type'] == "static":
                static_ip_range = group_config.get('static_ip_range', [])
                if static_ip_range:
                    available_ips = [ip for ip in static_ip_range if ip not in current_instances]
                    if available_ips:
                        ip_address = available_ips[0]
                        run_command(f"pct set {new_ctid} -net0 name=eth0,bridge=vmbr0,ip={ip_address}/24")
                    else:
                        logging.warning("No available IPs in the specified range for static IP assignment.")

            # Start the new container
            run_command(f"pct start {new_ctid}")
            current_instances.append(new_ctid)

            # Update the configuration and tracking
            group_config['lxc_containers'] = set(map(str, current_instances))
            scale_last_action[group_name] = datetime.now()

            logging.info(f"Container {new_ctid} started successfully as part of {group_name}.")
            send_notification(f"Scale Out: {group_name}", f"New container {new_ctid} with hostname {clone_hostname} started.")

            # Log the scale-out event to JSON
            log_json_event(new_ctid, "Scale Out", f"Container {base_snapshot} cloned to {new_ctid}. {new_ctid} started.")
        else:
            logging.error(f"Failed to clone container {base_snapshot} using snapshot {unique_snapshot_name}.")
    else:
        logging.error(f"Failed to create snapshot {unique_snapshot_name} of container {base_snapshot}.")

def is_off_peak():
    """
    Determine if the current time is within off-peak hours.

    Returns:
        bool: True if it is off-peak, otherwise False.
    """
    current_hour = datetime.now().hour
    return DEFAULTS['off_peak_start'] <= current_hour or current_hour < DEFAULTS['off_peak_end']

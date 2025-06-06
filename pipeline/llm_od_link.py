import numpy as np
import pandas as pd
import subprocess
from utils import get_error, extrac_column_info
import torch
from transformers import LlamaForCausalLM, AutoTokenizer, AutoModelForCausalLM
import timeit
import random
import os
import datetime
from dotenv import load_dotenv
from huggingface_hub import hf_hub_download
import re
import json
from pathlib import Path
import shutil
#from vllm import LLM, SamplingParams

# configure env keys
load_dotenv()
HUGGING_FACE_API_KEY = os.getenv("HUGGING_FACE_API_KEY")
experience_mode = 'column_experi' # ["record_best", "column_experi", "origin_data"]
np.set_printoptions(threshold=np.inf)

# load directories
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# define the path to the executable
exe_path = os.path.join(PROJECT_ROOT, config["paths"]["exe_path"])
data_path = os.path.join(PROJECT_ROOT, config["paths"]["data_path"])
working_dir = os.path.join(PROJECT_ROOT, config["paths"]["working_dir"])
link_performance_csv = os.path.join(PROJECT_ROOT, config["paths"]["link_performance"])
link_perform_odlink = os.path.join(PROJECT_ROOT, config["paths"]["link_perform_odlink"])
initial_demand_xlsm = os.path.join(PROJECT_ROOT, config["paths"]["initial_demand_xlsm"])
gt_path = os.path.join(PROJECT_ROOT, config["paths"]["gt_data"])
with open(gt_path, "r") as f:
    gt_json = json.load(f)
timestamp = config["timestamp"]
gt_entries = gt_json[timestamp]
gt_dict = {str(e["link_id"]): e["obs_count"] for e in gt_entries}
results_path = os.path.join(PROJECT_ROOT, "results", timestamp)
if not os.path.exists(results_path):
    os.makedirs(results_path, exist_ok=True)

print(f"Writing all logs/results into {results_path}")
shutil.copy(initial_demand_xlsm, os.path.join(results_path, Path(initial_demand_xlsm).name))
shutil.copy(link_performance_csv, os.path.join(results_path, Path(link_performance_csv).name))
# Run the simulation executable
#subprocess.run(["wine64", exe_path], cwd=data_path)

#file_path  = initial_demand_xlsm
demand_csv = os.path.join(PROJECT_ROOT, "datasets", "demand", "demand_12_00_PM.csv")
demand_df = pd.read_csv(demand_csv, index_col=0)
#df_ini = extrac_column_info(file_path)
zone_labels = list(demand_df.index.astype(str))
initial_matrix = demand_df.to_numpy(dtype=float)
np.fill_diagonal(initial_matrix, 0.0)
current_matrix = initial_matrix.copy()

def run_simulation(matrix):
    """
    Write out matrix to demand.csv, run simulator and pipelines,
    and overwrite link_performance_csv with those new volumes.
    """
    # 1) write the demand file
    pd.DataFrame(matrix, index=zone_labels, columns=zone_labels) \
      .to_csv(os.path.join(data_path, "demand.csv"))

    # 2) run the external exe + two Python pipelines for lp data
    subprocess.run(["wine64", exe_path], cwd=data_path, check=True)
    subprocess.run(["python3", "pipeline/od_link_mapping_route.py"], 
                   cwd=PROJECT_ROOT, check=True)
    subprocess.run(["python3", "pipeline/update_lp_odlink.py"], 
                   cwd=PROJECT_ROOT, check=True)

def calculate_mse(matrix, data_path, exe_path, results_path, link_performance_csv, gt_dict): 
    # matrix: 56 * 56
    # Convert matrix to demand file and run simulation
    run_simulation(matrix)
    df_sim = pd.read_csv(link_performance_csv, usecols=["link_id", "volume"])
    df_sim["link_id"] = df_sim["link_id"].astype(str)
    df_valid = df_sim[df_sim["link_id"].isin(gt_dict)]
    sim_vol = df_valid["volume"].astype(float).to_numpy()
    gt_vol  = df_valid["link_id"].map(gt_dict).astype(float).to_numpy()

    # ignore any zero on either side like get_error() did
    no_zeros = (sim_vol != 0) & (gt_vol != 0)
    sim_vol = sim_vol[no_zeros]
    gt_vol  = gt_vol [no_zeros]

    if sim_vol.size == 0:
        raise RuntimeError("No matching non‐zero link volumes to compute MSE")

    mse = np.mean((sim_vol - gt_vol) ** 2)
    
    mse_history_file = os.path.join(results_path, "mse_history.txt")
    with open(mse_history_file, 'a+') as file:
        file.write(str(mse)+"\n")
    return mse

# returns a dict with abs errors of all links
def calculate_abs_error(link_performance_csv, gt_dict):
    df = pd.read_csv(link_performance_csv, usecols=["link_id", "volume"])
    df["link_id"] = df["link_id"].astype(str)

    abs_errors = {}
    for link_id, sim_vol in zip(df["link_id"], df["volume"]):
        gt = gt_dict.get(link_id)
        if gt is None or pd.isna(sim_vol):   # skip if GT missing or NaN
            continue
        abs_errors[link_id] = abs(float(sim_vol) - float(gt))

    # return link_ids sorted by descending error
    return sorted(abs_errors, key=abs_errors.get, reverse=True)

# returns abs error for a particular link id
def get_abs_error(link_id, link_performance_csv, gt_dict):
    df = pd.read_csv(link_performance_csv, usecols=["link_id", "volume"])
    df["link_id"] = df["link_id"].astype(str)
    row = df[df["link_id"] == str(link_id)]
    if row.empty or str(link_id) not in gt_dict:
        return float("inf")
    return abs(float(row.iloc[0]["volume"]) - float(gt_dict[str(link_id)]))

# returns simulated_volume and obs_count (ground truth) for the given link_id
def get_link_data(link_id, link_performance_csv, gt_dict):
    df = pd.read_csv(link_performance_csv, usecols=["link_id", "volume"])
    df["link_id"] = df["link_id"].astype(str)
    row = df[df["link_id"] == str(link_id)]
    if row.empty or str(link_id) not in gt_dict:
        return None, None
    return float(row.iloc[0]["volume"]), float(gt_dict[str(link_id)])

# sample od pairs link_id and path to the odlink
def sample_od_pairs(link_id, odlink_csv, current_matrix,
                    top_k_candidates: int, sample_k: int):
    df = pd.read_csv(odlink_csv, dtype=str)
    row = df.loc[df['link_id']==link_id]
    if row.empty:
        return []

    raw = row.iloc[0].get('od_pairs',"")
    # guard against NaN or non-strings
    od_str = "" if pd.isna(raw) else str(raw)

    parsed = re.findall(r"\((\d+),\s*(\d+)\)", od_str)
    if not parsed:
        return []

    # build list [((i,j),flow),…]
    pairs = []
    for i_s,j_s in parsed:
        i,j = int(i_s), int(j_s)
        if 0 <= i < current_matrix.shape[0] and 0 <= j < current_matrix.shape[1]:
            pairs.append(((i,j), float(current_matrix[i,j])))
    if not pairs:
        return []

    # 1) sort by flow descending
    pairs.sort(key=lambda x: x[1], reverse=True)
    # 2) candidate pool
    candidates = pairs[: min(top_k_candidates, len(pairs))]
    k = min(sample_k, len(candidates))
    # 3) random (uniform) sampling
    return random.sample(candidates, k)

# load LLaMa 3.3-70B model
model_name = "meta-llama/Llama-3.3-70B-Instruct"
tokenizer = AutoTokenizer.from_pretrained(
    model_name, 
    use_auth_token=HUGGING_FACE_API_KEY
)

""" llm = LLM(
    model=model_name,
    tensor_parallel_size=2,
    dtype="float16",               # FP16 inference
) """

# Initialize an empty model
model = AutoModelForCausalLM.from_pretrained(
    model_name, 
    device_map="auto", 
    torch_dtype=torch.float16)
model.eval()

def model_prompt(link_id, abs_error, sampled_od_pairs, simulated_vol, obs_count, top_k_candidates, sample_k):
    """
    sampled_od_pairs is now a list of [((i_int, j_int), flow_val), ...]
    """
    pairs_str = ""
    for (i_int, j_int), flow_val in sampled_od_pairs:
        pairs_str += f"({i_int},{j_int}), {flow_val:.2f}\n"
    sample_size = len(sampled_od_pairs)
    prompt = f"""
    System Description:
    We are calibrating a 56x56 Origin-Destination (OD) matrix for a transportation network. Each entry [i, j] represents the number of trips from origin i to destination j. 
    This OD matrix is used in a simulation to generate traffic volume counts on various links in the network.

    You will be provided with the following details of one link:
    Link ID: {link_id}
    The simulated volume: {simulated_vol}
    The ground truth volume: {obs_count}
    The absolute error, which is calculated as: abs(Simulated Volume - Ground Truth Volume) = {abs_error}

    OD Pair Sampling Details:
    - From the top {top_k_candidates} highest-flow OD elements contributing to this link, we have randomly sampled {sample_k} OD pairs for you to adjust.
    - These {sample_size} OD pairs and their current flow values are listed below:
    {pairs_str}

    Your Task:
    Adjust ONLY these OD elements' flow values to reduce the absolute error, thereby improving the alignment between the simulation results and real-world traffic observations.

    Response Constraints and Format:
    - Do not return any placeholder text.
    - Return ONLY the updated values of the {sample_size} OD elements with their indices, one per line, in the format:
      [(i, j), new_value]
      [(i2, j2), new_value_2]
      ...

    """
    return prompt

def parse_llm_output(model_output):
    # assuming model output is in the form
    lines = model_output.strip().split("\n")
    parsed_data = {}

    pattern = re.compile(r'^\[\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*,\s*([\d\.]+)\]$')
    # iterate through all lines to parse through output
    for line in lines:
        line = line.strip()
        match = pattern.match(line)
        if match:
            i_str, j_str, val_str = match.groups()
            i = int(i_str)
            j = int(j_str)
            new_value = float(val_str)  # parse as float in case of decimals
            parsed_data[(i, j)] = new_value
        else:
            print(f"Skipping line that doesn't match format: {line}")
            pass
    return parsed_data

def generate_output(prompt, model, tokenizer, working_dir, link_id, attempt, results_path):
    matrix_start = timeit.default_timer()
    inputs = tokenizer(prompt, return_tensors = "pt").to("cuda")

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens = 1000,
            temperature = 0.7,
            do_sample = True,
            top_p=0.9,
            top_k=50,
        )

    generated_text = tokenizer.decode(outputs[0], skip_special_tokens=True)
    logs_path = os.path.join(results_path, f"logs")
    if not os.path.exists(logs_path):
        os.makedirs(logs_path, exist_ok=True)
    output_path = os.path.join(logs_path, f"llama_raw_output{link_id}_attempt{attempt}.txt")
    with open(output_path, "w+") as f:
        f.write(generated_text)
    # parse llm output
    output_od_pairs = parse_llm_output(generated_text)
    matrix_stop = timeit.default_timer()
    total_matrix = matrix_stop - matrix_start
    print(f"Time taken to generate output: {total_matrix}")
    return output_od_pairs

def update_od_matrix(od_matrix, updated_pairs):
    new_matrix = od_matrix.copy()
    for (i,j), val in updated_pairs.items():
        if i < 0 or i >= 56 or j < 0 or j >= 56:
            print(f"Warning: LLM suggested an out-of-bounds index ({i}, {j}). Skipping this update.")
            continue
        new_matrix[i][j] = max(0.0, val)
    return new_matrix

def log_improvement_and_save(
    results_path, 
    global_iter, 
    link_id, 
    attempt, 
    old_mse, 
    new_mse, 
    updated_pairs, 
    best_matrix
):
    # 1. write detailed improvement log
    log_path = os.path.join(results_path, "detailed_log.txt")
    with open(log_path, "a+") as log_file:
        log_file.write("=== SUCCESSFUL ITERATION ===\n")
        log_file.write(f"Global Iteration: {global_iter}\n")
        log_file.write(f"Link: {link_id}\n")
        log_file.write(f"Attempt: {attempt+1}\n")  # Add +1 if attempt is zero-based
        log_file.write(f"Old MSE: {old_mse:.4f}, New MSE: {new_mse:.4f}\n")
        log_file.write("Updated OD Pairs (i,j -> new_value):\n")
        for (i, j), val in updated_pairs.items():
            log_file.write(f"  ({i},{j}) -> {val:.2f}\n")
        log_file.write("\n")

    # save current best matrix
    matrix_path = os.path.join(results_path, "best_matrix_current.csv")
    with open(matrix_path, "w") as csv_file:
        for row in best_matrix:
            row_str = ",".join(str(int(x)) for x in row)
            csv_file.write(row_str + "\n")
    print(f"Saved best_matrix to {matrix_path}")

# LLM Optimization Pipeline
baseline_matrix = initial_matrix.copy()
baseline_mse = calculate_mse(baseline_matrix, data_path, exe_path, results_path, link_performance_csv, gt_dict)
best_mse = baseline_mse
best_matrix = baseline_matrix.copy()
print(f"Baseline MSE: {baseline_mse}")
sorted_links = calculate_abs_error(link_performance_csv, gt_dict)
k = config["hyperparams"]["top_n_links"]
top_links = sorted_links[:k] # top k links
print(f"Top {k} links with highest abs error: {top_links}")
current_matrix = baseline_matrix.copy()

num_iterations = config["hyperparams"]["num_iterations"] # number of attempts per link
# in each global iteration, if a link has failed to optimize we add it here to this dictionary
max_global_iterations = config["hyperparams"]["max_global_iterations"] # how many times you want to re-check top errors overall
max_fail_passes = config["hyperparams"]["max_fail_passes"] # currently set as 2
sample_k = config["hyperparams"]["sample_k"]
top_k_candidates = config["hyperparams"]["top_k_candidates"]
fail_pass_count = {}
no_improvement_links = set()
# for plotting purposes, store the MSE each time it improves
improvements_list = []

early_stop = False # performance checker flag
for global_iter in range(max_global_iterations):
    # check if we converged around 27k to compare to genetic algorithm
    if early_stop:
        break
    print(f"Global iteration: {global_iter}")
    baseline_mse = calculate_mse(  # calculate_mse will re-run sim and need to compute MSE
        current_matrix,
        data_path, exe_path, results_path,
        link_performance_csv, gt_dict
    )
    print(f"Baseline MSE: {baseline_mse:.4f}")
    # ensures to check sorted links after improvement was found. this is because simulated volume keeps changing
    sorted_links = calculate_abs_error(link_performance_csv, gt_dict)
    sorted_links = [lk for lk in sorted_links if lk not in no_improvement_links] # if sorted link is in the not improved list, skip it
    if not sorted_links:
        print("No valid links remain for calibration. Exiting.")
        break  # exits the global_iter loop

    top_links = sorted_links[:k] # top k links
    print(f"Top {k} links with highest abs error: {top_links}")
    #improvement_this_cycle = False
    # iterate through top links
    for link_id in top_links:
        improvement_found = False
        if link_id in no_improvement_links:
            print(f"Skipping link {link_id} due to repeated failures.")
            continue

        #  attempts for each link
        for attempt in range(num_iterations):
            print(f"\nLink: {link_id}, Attempt: {attempt+1} of {num_iterations}")
            sampled_od_pairs = sample_od_pairs(link_id, link_perform_odlink, current_matrix, top_k_candidates, sample_k)
            if not sampled_od_pairs:
                print(f"No OD pairs found for link {link_id}. Skipping.")
                break
            # get absolute error for that link id and read current baseline volumes
            simulated_vol, obs_count = get_link_data(link_id, link_performance_csv, gt_dict)
            if simulated_vol is None or obs_count is None:
                print(f"Skipping link {link_id} due to missing data.")
                continue
            
            abs_error = abs(simulated_vol - obs_count)
            prompt = model_prompt(link_id, abs_error, sampled_od_pairs, simulated_vol, obs_count, top_k_candidates, sample_k)
            # pass everything to llm and output the dictionary
            llm_output_dict = generate_output(prompt, model, tokenizer, working_dir, link_id, attempt, results_path) 
            updated_pairs = llm_output_dict
            # update od matrix with new updated i,j pairs
            test_matrix = update_od_matrix(current_matrix, updated_pairs) 
            new_mse = calculate_mse(test_matrix, data_path, exe_path, results_path, link_performance_csv, gt_dict) # recalculate mse by running simulation
            
            # is there an improvement
            if new_mse < baseline_mse:
                print(f"Improvement found for link {link_id}: MSE improved from {baseline_mse:.4f} to {new_mse:.4f}")
                current_matrix = test_matrix.copy()
                best_matrix = current_matrix
                old_mse = baseline_mse
                baseline_mse = new_mse
                best_mse = baseline_mse
                improvements_list.append(new_mse)
                improvement_found = True
                # log values
                log_improvement_and_save(
                    results_path,
                    global_iter=global_iter,
                    link_id=link_id,
                    attempt=attempt,
                    old_mse=old_mse,
                    new_mse=new_mse,
                    updated_pairs=updated_pairs,
                    best_matrix=best_matrix
                )
                break
            else:
                run_simulation(current_matrix)
        if not improvement_found:
            print(f"No improvement found for link {link_id} after {num_iterations} attempts. Moving to next link.\n")
            # increase failure count for this link
            fail_pass_count[link_id] = fail_pass_count.get(link_id, 0) + 1
            # if it has failed too many times, add to the skip set
            if fail_pass_count[link_id] >= max_fail_passes:
                no_improvement_links.add(link_id)
        else:
            print(f"Improvement found for link {link_id}. Moving on.")
            #break
    """ if not improvement_this_cycle:
        print(f"No improvements in global iteration {global_iter}. Stopping calibration.")
        break """

mse_val_path = os.path.join(results_path, "improvements.txt")
with open(mse_val_path, "a+") as f:
    for val in improvements_list:
        f.write(str(val) + "\n")

print("Recorded MSE improvements:", improvements_list)
print("LLM optimization finished.")
print(f"Final best MSE: {best_mse}")
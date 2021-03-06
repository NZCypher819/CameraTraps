"""

    manage_api_submission.py
    
    Semi-automated process for submitting and managing camera trap API tasks.
    
    Terminology:
    - taskgroup: a group of requests
        usually 1 Azure Blob Storage container = 1 taskgroup, but we can also
        specify individual folders inside a container to constitute a taskgroup
    - request: an individual call to the Batch Processing API, also known as a task
    - task: a request
    
"""

"""

# Not yet automated:
#
# * Mounting the image source, only necessary for folder splitting or occasional
#   sanity-checking.  But FWIW, here's the gist of it:
#
#   import clipboard; clipboard.copy(read_only_sas_url)
#   configure mount point with rclone config
#   rclone mount mountname: z:
#
# * Pushing the final results to shared storage and generating a SAS URL to
#   share with the collaborator
#
# * Pushing the previews to shared storage
#

"""

#%% Imports

import json
import ntpath
import os
import posixpath
import pprint
import time
import itertools

from urllib.parse import urlsplit, unquote
from typing import Any, Dict, List
from tqdm import tqdm

import clipboard
import humanfriendly

import ai4e_azure_utils  # from ai4eutils
import path_utils  # from ai4eutils
import sas_blob_utils  # from ai4eutils

from api.batch_processing.data_preparation import prepare_api_submission
from api.batch_processing.postprocessing import combine_api_outputs
from api.batch_processing.postprocessing.postprocess_batch_results import (
    PostProcessingOptions, process_batch_results)


#%% Constants

max_task_name_length = 92
submission_time_between_tasks = 60


#%% Constants I set per script

### Required

storage_account_name = 'blah'
container_name = 'blah'
base_task_name = 'institution-20191215'
base_output_folder_name = r'f:\institution'

# Shared Access Signature (SAS) tokens for the Azure Blob Storage container.
# Leading question mark is optional.
#
# The read-only token is used for accessing images; the write-enabled token is
# used for writing file lists.  As of 2020.12.07, these tokens cannot use access policies.
read_only_sas_token = '?sv=2019-12...'
read_write_sas_token = '?sv=2019-12...'

caller = 'caller'
endpoint_base = 'http://blah.endpoint.com:6022/v3/camera-trap/detection-batch'

### Typically left as default

container_prefix = ''

# This is how we break the container up into multiple taskgroups, e.g., for
# separate surveys. The typical case is to do the whole container as a single
# taskgroup.
folder_names = [''] # ['folder1', 'folder2', 'folder3']

# If your "folders" are really logical folders corresponding to multiple folders,
# map them here
folder_prefixes = None # {'stuff':['a','b','c']}

# This is only necessary if you will be performing postprocessing steps that
# don't yet support SAS URLs, specifically the "subsetting" step, or in some
# cases the splitting of files into multiple output directories for
# empty/animal/vehicle/people.
#
# For those applications, you will need to mount the container to a local drive.
# For this case I recommend using rclone whether you are on Windows or Linux;
# rclone is much easier than blobfuse for transient mounting.
#
# But most of the time, you can ignore this.
image_base = 'x:\\'

additional_task_args: Dict[str, Any] = {}

# Supported model_versions: '4', '3', '4_prelim'
#
# Also available at the /supported_model_versions and /default_model_version
# endpoints
#
# Unless you have any specific reason to set this to a non-default value, leave
# it at the default, which as of 2020.04.28 is MegaDetector 4.1
#
# additional_task_args = {"model_version":"4_prelim"}
#


#%% Derived variables, path setup

assert len(folder_names) != 0

read_only_sas_url = sas_blob_utils.build_azure_storage_uri(
    account=storage_account_name, container=container_name,
    sas_token=read_only_sas_token)
write_sas_url = sas_blob_utils.build_azure_storage_uri(
    account=storage_account_name, container=container_name,
    sas_token=read_write_sas_token)

# local folders
filename_base = os.path.join(base_output_folder_name, base_task_name)
raw_api_output_folder = os.path.join(filename_base, 'raw_api_outputs')
combined_api_output_folder = os.path.join(filename_base, 'combined_api_outputs')
postprocessing_output_folder = os.path.join(filename_base, 'postprocessing')

os.makedirs(filename_base, exist_ok=True)
os.makedirs(raw_api_output_folder, exist_ok=True)
os.makedirs(combined_api_output_folder, exist_ok=True)
os.makedirs(postprocessing_output_folder, exist_ok=True)

# Turn warnings into errors if more than this many images are missing
max_tolerable_missing_images = 20


#%% Support functions

def url_to_filename(url):
    """
    See: https://gist.github.com/zed/c2168b9c52b032b5fb7d
    """
    # scheme, netloc, path, query, fragment
    urlpath = urlsplit(url).path

    basename = posixpath.basename(unquote(urlpath))
    if (os.path.basename(basename) != basename or
            unquote(posixpath.basename(urlpath)) != basename):
        raise ValueError  # reject '%2f' or 'dir%5Cbasename.ext' on Windows

    return basename


#%% Enumerate blobs to files

# file_lists_by_folder will contain a list of local JSON file names,
# each JSON file contains a list of blob names corresponding to an API taskgroup
file_lists_by_folder = []

# A flat list of blob paths for each folder
images_by_folder = []

# folder_name = folder_names[0]
for folder_name in folder_names:
    
    clean_folder_name = path_utils.clean_filename(folder_name)
    json_filename = '{}_{}_all.json'.format(base_task_name,clean_folder_name)
    list_file = os.path.join(filename_base, json_filename)

    prefix = container_prefix + folder_name
    
    # Handle the case where a "folder" is really a list of folders
    rsearch = None
    if folder_prefixes is not None:
        prefix = container_prefix
        rsearch = []
        prefix_list = folder_prefixes[folder_name]
        for p in prefix_list:
            rsearch.append('^' + p)
                    
    # If this is intended to be a folder, it needs to end in '/', otherwise
    # files that start with the same string will match too
    folder_name = folder_name.replace('\\', '/')
    if len(folder_name) > 0 and (not folder_name.endswith('/')):
        folder_name = folder_name + '/'
                
    images = ai4e_azure_utils.enumerate_blobs_to_file(
        output_file=list_file,
        account_name=storage_account_name,
        container_name=container_name,
        sas_token=read_only_sas_token,
        blob_prefix=prefix,
        rsearch=rsearch)
    
    file_lists_by_folder.append(list_file)
    images_by_folder.append(images)

# ...for each folder

assert len(file_lists_by_folder) == len(folder_names)


#%% Some just-to-be-safe double-checking around enumeration

# Make sure each folder has at least one image matched; the opposite is usually a sign of a copy/paste issue

all_images = list(itertools.chain.from_iterable(images_by_folder))

for folder_name in folder_names:
    
    if folder_prefixes is not None:
        prefixes = folder_prefixes[folder_name]
    else:
        prefixes = [folder_name]
        
    for p in prefixes:
        
        found_image = False
        
        for fn in all_images:
            if fn.startswith(p):
                found_image = True
                break
        # ...for each image
            
        assert found_image, 'Could not find image for prefix {}'.format(p)
        
    # ...for each prefix
    
# ...for each folder

# Make sure each image comes from one of our folders; the opposite is usually a sign of a bug up above
        
for fn in tqdm(all_images):
    
    found_folder = False
    
    for folder_name in folder_names:
        
        if folder_prefixes is not None:
            prefixes = folder_prefixes[folder_name]
        else:
            prefixes = [folder_name]
            
        for p in prefixes:        
            
            if fn.startswith(p):
                found_folder = True
                break
            
        # ...for each prefix
            
        if found_folder:
            break
    
    # ...for each folder
        
    assert found_folder, 'Could not find folder for image {}'.format(fn)

# ...for each image
        
        
#%% Divide images into chunks for each folder

# The JSON file at folder_chunks[i][j] corresponds to task j of taskgroup i
folder_chunks = []

# list_file = file_lists_by_folder[0]
for list_file in file_lists_by_folder:
    chunked_files, chunks = prepare_api_submission.divide_files_into_tasks(
        list_file)
    print('Divided images for task {} into files:'.format(list_file))
    for i_fn, fn in enumerate(chunked_files):
        new_fn = chunked_files[i_fn].replace('__', '_').replace('_all', '')
        os.replace(fn, new_fn)
        chunked_files[i_fn] = new_fn
        print(fn, len(chunks[i_fn]))
    folder_chunks.append(chunked_files)

assert len(folder_chunks) == len(folder_names)


#%% Create taskgroups and tasks, and upload image lists to blob storage

task_names = set()
taskgroups: List[List[prepare_api_submission.Task]] = []

for i, taskgroup_json_paths in enumerate(folder_chunks):

    taskgroup = []
    for j, task_json_path in enumerate(taskgroup_json_paths):

        # periods not allowed in task names
        task_json_filename = ntpath.basename(task_json_path)
        task_json_filename_root = os.path.splitext(task_json_filename)[0]
        task_name = '{}_{}'.format(base_task_name,task_json_filename_root).replace(
            '.', '_')
        if len(task_name) > max_task_name_length:
            long_task_name = task_name
            task_name = task_name[:max_task_name_length]
            print('Warning: task name {} too long, shortened to {}'.format(
                long_task_name,task_name))                  
        assert task_name not in task_names
        task_names.add(task_name)
        task = prepare_api_submission.Task(
            name=task_name, images_list_path=task_json_path,
            api_url=endpoint_base)

        blob_name = 'api_inputs/{}/{}'.format(base_task_name,task_json_filename)
        print('Task {}: uploading {} to {}'.format(
            task_name,task_json_path,blob_name))
        task.upload_images_list(
            account=storage_account_name, container=container_name,
            sas_token=read_write_sas_token, blob_name=blob_name)

        taskgroup.append(task)

    taskgroups.append(taskgroup)

assert len(taskgroups) == len(folder_names)


#%% Generate API calls for each task

request_strings = []

for taskgroup in taskgroups:
    for task in taskgroup:
        request = task.generate_api_request(
            caller=caller,
            input_container_url=read_only_sas_url,
            image_path_prefix=None,
            **additional_task_args)
        request_str = json.dumps(request, indent=1)
        request_strings.append(request_str)

pprint.pprint(request_strings)

# clipboard.copy(request_strings[0])
clipboard.copy('\n\n'.join(request_strings))


#%% Run the tasks (don't run this cell unless you are absolutely sure!)

# I really want to make sure I'm sure...
if False:
    
    for i_taskgroup,taskgroup in enumerate(taskgroups):
        for i_task,task in enumerate(taskgroup):
            task_id = task.submit()
            print('Submitted task {} ({})'.format(task.name, task_id))
            if not ((i_taskgroup == len(taskgroups)-1) and (i_task == len(taskgroup)-1)):
                print('Sleeping to be extra-safe about rapid submissions')
                time.sleep(submission_time_between_tasks)                


#%% Estimate total time

n_images = 0
for fn in file_lists_by_folder:
    with open(fn, 'r') as f:
        images = json.load(f)
    n_images += len(images)

print('Processing a total of {} images'.format(n_images))

# Around 0.8s/image on 16 GPUs
expected_seconds = (0.8 / 16) * n_images
print('Expected time: {}'.format(humanfriendly.format_timespan(expected_seconds)))


#%% Manually define task groups if we ran the tasks manually

if False:

    #%%    

    # For just one task...
    taskgroup_ids = [["9999"]]
    
    # For multiple tasks...
    # taskgroup_ids = [["1111"], ["2222"], ["3333"]]
    
    for i, taskgroup in enumerate(taskgroups):
        for j, task in enumerate(taskgroup):
            task.id = taskgroup_ids[i][j]


#%% Status check

for taskgroup in taskgroups:
    for task in taskgroup:
        response = task.check_status()
        print(response)
        # print(task.id)


#%% Automatically pull results from the AzureML output if output got stalled
        
if False:

    #%%        
    
    import sas_blob_utils
    
    api_container_info_file = r'g:\api_container_info.txt'
    if api_container_info_file is None:
        api_result_container_sas = '?sv='
        api_result_storage_account = ''
        api_result_container_name = ''
    else:
        with open(api_container_info_file,'r') as f:
            api_container_info = f.readlines()
            api_container_info = [s.strip() for s in api_container_info]
            api_result_container_sas = api_container_info[0]
            api_result_storage_account = api_container_info[1]
            api_result_container_name = api_container_info[2]
    
    container_uri = sas_blob_utils.build_azure_storage_uri(
                    account=api_result_storage_account, container=api_result_container_name, sas_token=api_result_container_sas)
    
    #%%
    
    if False:
        
        #%% 
        
        # Use this when you don't know the task ID, typically because this notebook closed
        
        matched_blobs = sas_blob_utils.list_blobs_in_container(container_uri=container_uri)
        task_blobs = [s for s in matched_blobs if base_task_name in s]
        task_id = task_blobs[0].split('/')[0]
        matched_blobs = sas_blob_utils.list_blobs_in_container(container_uri=container_uri,blob_prefix=task_id)
    
        task_to_results = {}
        task_to_results[task_id] = matched_blobs
        
        taskgroups = None
        
        #%%
        
    if False:
        
        #%%
        
        # Use this when you still have the taskgroups and Task IDs, typically because
        # this notebook is fine, but the job stalled

        # Maps task IDs to lists of resulting blobs
        task_to_results = {}
        
        # Enumerate files associated with each task
        #
        # taskgroup = taskgroups[0]; task = taskgroup[0]
        for taskgroup in taskgroups:
            for task in taskgroup:
                
                task_id = task.id
                
                # Enumerate files associated this this task
                matched_blobs = sas_blob_utils.list_blobs_in_container(container_uri=container_uri, blob_prefix=task_id)
                task_to_results[task_id] = matched_blobs
        
        #%%
                
    #%% 
            
    # Determine which tasks have finished, build completion messages for each
            
    # task_id = (list(task_to_results.keys()))[0];
    task_id_to_msg = {}        
    for task_id in task_to_results:
        
        results = task_to_results[task_id]
        
        if len(results) != 3:
            print('Task {} is not finished'.format(task_id))
            task_id_to_msg[task_id] = None
            continue
        
        images_url = ''
        failed_images_url = ''
        detections_url = ''
        
        # s = results[0]
        task_id_start = task_id[0:8]
        for s in results:
            assert s.startswith(task_id_start)
            s_url = container_uri.replace('?','/' + s + '?')
            blob_name = s.split('/')[1]
            if blob_name.endswith('_images.json'):
                images_url = s_url
            elif blob_name.startswith(task_id_start + '_detections'):
                detections_url = s_url
            elif blob_name.startswith(task_id_start + '_failed_images'):    
                failed_images_url = s_url
            else:
                raise ValueError('Cannot map blob {}'.format(s))
        
            msg = prepare_api_submission.create_response_message(0,
                                    failed_images_url=failed_images_url,
                                    images_url=images_url,
                                    detections_url=detections_url,
                                    task_id=task_id)
            
        # ...for each blob in this folder
        
        task_id_to_msg[task_id] = msg
        
    # ...for each task
    

    #%% Replace task group tasks
    
    if False:
        
        #%%
        
        # Use this if you're building taskgroups from scratch, typically because this notebook closed
        
        msg = task_id_to_msg[task_id]
        new_task = prepare_api_submission.Task(name=task_id + '_reprise',task_id=msg['request_id'],
                                        api_url=endpoint_base,validate=False)
        new_task.force_completion(msg) 
        taskgroups = [[new_task]]

        #%%
        
    if False:
        
        #%%
        
        # Use this if you still have taskgroups, typically because the job stalled

        for taskgroup in taskgroups:
        
            for i_task,task in enumerate(taskgroup):
                
                task_id = task.id
                msg = task_id_to_msg[task_id]
                new_task = prepare_api_submission.Task(name=task_id + '_reprise',task_id=msg['request_id'],
                                                api_url=endpoint_base,validate=False)
                new_task.force_completion(msg) 
                taskgroup[i_task] = new_task
            
            # ...for each task
                
        # ...for each task group
                        
                
#%% Look for failed shards or missing images, start new tasks if necessary

n_resubmissions = 0

# This will be a list of lists of tasks, with either one or zero
# elements per taskgroup.
resubmitted_tasks = []

# List of lists of paths.  Neither are used explicitly, but are handy for debugging.
missing_images_by_task = []
failed_images_by_task = []

# i_taskgroup = 0; taskgroup = taskgroups[i_taskgroup];
for i_taskgroup, taskgroup in enumerate(taskgroups):

    resubmitted_tasks_this_taskgroup = []
    
    # Make a copy, because we append to taskgroup
    tasks = list(taskgroup)  
    
    # i_task = 0; task = tasks[i_task]
    for i_task,task in enumerate(tasks):
            
        print('\n*** Task {} ({} in taskgroup {}) ***\n'.format(task.id,i_task,i_taskgroup))
        
        response = task.check_status()

        n_failed_shards = response['Status']['message']['num_failed_shards']
        if n_failed_shards != 0:
            print('Warning: {} failed shards for task {}'.format(n_failed_shards,task.id))
            
        output_file_urls = task.get_output_file_urls()
        detections_url = output_file_urls['detections']
        detections_fn = url_to_filename(detections_url)

        # Each taskgroup corresponds to one of our folders
        folder_name = folder_names[i_taskgroup]
        clean_folder_name = prepare_api_submission.clean_request_name(
            folder_name)
        assert (folder_name in detections_fn) or (clean_folder_name in detections_fn)
        assert 'chunk' in detections_fn

        missing_images_fn = os.path.join(
            raw_api_output_folder, detections_fn.replace('.json', '_missing.json'))
        missing_images = task.get_missing_images(verbose=True)
        missing_images_by_task.append(missing_images)
        ai4e_azure_utils.write_list_to_file(missing_images_fn, missing_images)
        
        failed_images_fn = os.path.join(
            raw_api_output_folder, detections_fn.replace('.json', '_failed.json'))
        failed_images_url = task.get_output_file_urls()['failed_images']
        prepare_api_submission.download_url(failed_images_url, failed_images_fn)
        with open(failed_images_fn,'r') as failf:
            failed_images = json.load(failf)
            assert isinstance(failed_images,list)
        failed_images_by_task.append(failed_images)
        
        num_missing_images = len(missing_images)
        if num_missing_images < max_tolerable_missing_images:
            continue

        print('Warning: {} missing images for task {}'.format(len(missing_images),task.id))
        task_name = '{}_{}_{}_missing_images'.format(base_task_name,folder_name,task.id)
        blob_name = 'api_inputs/{}/{}.json'.format(base_task_name,task_name)
        new_task = prepare_api_submission.Task(
            name=task_name, images_list_path=missing_images_fn,
            api_url=endpoint_base)
        print('Task {}: uploading {} to {}'.format(task_name,missing_images_fn,blob_name))
        new_task.upload_images_list(
            account=storage_account_name, container=container_name,
            blob_name=blob_name, sas_token=read_write_sas_token, overwrite=True)
        request = new_task.generate_api_request(
            caller=caller, input_container_url=read_only_sas_url,
            image_path_prefix=None, **additional_task_args)

        # Do not append here; do this manually in the next cell if we actually decide
        # to run the resubmitted task.
        # taskgroup.append(new_task)
        resubmitted_tasks_this_taskgroup.append(new_task)

        # automatic submission
        # new_task.submit()

        # manual submission
        print('\nResbumission string for task {}:\n'.format(task_id))
        print(json.dumps(request, indent=1))
        print('')

        n_resubmissions += 1

    # ...for each task

    resubmitted_tasks.append(resubmitted_tasks_this_taskgroup)
    
# ...for each task group

if n_resubmissions == 0:
    print('No resubmissions necessary')


#%% See what's up with failed/missing images (debugging cell)
    
if False:

    #%%
    
    n_missing = sum([len(x) for x in missing_images_by_task])
    n_failed = sum([len(x) for x in failed_images_by_task])
    print('{} missing images total ({} failed)'.format(n_missing,n_failed))
    
    #%%
    
    failed_images_flat = list(itertools.chain.from_iterable(failed_images_by_task))
    i_image = 100
    sample_image_path = failed_images_flat[i_image]
    url = read_only_sas_url.replace('?','/'+sample_image_path+'?')
    clipboard.copy(url)
    

#%% Resubmit tasks for failed shards, add to appropriate task groups

if False:

    #%%
    
    for taskgroup in resubmitted_tasks:
        for task in taskgroup:
            response = task.check_status()
            print(response)
    
    #%%
        
    taskgroup_ids = [['2233', '9484', '1222'], ['1197', '1702', '2764']]

    for i, taskgroup in enumerate(taskgroups):
        for j, task in enumerate(taskgroup):
            if hasattr(task, 'id'):
                assert task.id == taskgroup_ids[i][j]
            else:
                task.id = taskgroup_ids[i][j]


#%% Pull results

task_id_to_results_file = {}

# i_taskgroup = 0; taskgroup = taskgroups[i_taskgroup]; task = taskgroup[0]
for i_taskgroup, taskgroup in enumerate(taskgroups):

    for task in taskgroup:

        output_file_urls = task.get_output_file_urls()
        detections_url = output_file_urls['detections']
        fn = url_to_filename(detections_url)

        # Each taskgroup corresponds to one of our folders
        folder_name = folder_names[i_taskgroup]
        clean_folder_name = prepare_api_submission.clean_request_name(
            folder_name)
        assert (folder_name in fn) or (clean_folder_name in fn)
        assert 'chunk' in fn or 'missing' in fn

        output_file = os.path.join(raw_api_output_folder, fn)
        print('Downloading {} to {}'.format(detections_url,output_file))
        prepare_api_submission.download_url(detections_url, output_file)
        task_id_to_results_file[task.id] = output_file

    # ...for each task

# ...for each task group


#%% Combine results from task groups into final output files

folder_name_to_combined_output_file = {}

for i_taskgroup, taskgroup in enumerate(taskgroups):

    folder_name_raw = folder_names[i_taskgroup]
    folder_name = path_utils.clean_filename(folder_name_raw)
    print('Combining results for {}'.format(folder_name))

    results_files = []
    for task in taskgroup:
        raw_output_file = task_id_to_results_file[task.id]
        results_files.append(raw_output_file)

    combined_api_output_file = os.path.join(
        combined_api_output_folder,
        '{}{}_detections.json'.format(base_task_name,folder_name))

    print('Combining the following into {}:'.format(combined_api_output_file))
    pprint.pprint(results_files)

    combine_api_outputs.combine_api_output_files(
        results_files, combined_api_output_file)
    folder_name_to_combined_output_file[folder_name] = combined_api_output_file

    # Check that we have (almost) all the images
    list_file = file_lists_by_folder[i_taskgroup]
    with open(list_file, 'r') as f:
        requested_images_set = set(json.load(f))
    with open(combined_api_output_file, 'r') as f:
        results = json.load(f)
        result_images_set = set(im['file'] for im in results['images'])
    missing_files = requested_images_set - result_images_set
    missing_images = path_utils.find_image_strings(missing_files)
    if len(missing_images) > 0:
        print('Warning: {} missing images for folder {}'.format(len(missing_images),folder_name))              
    assert len(missing_images) < max_tolerable_missing_images

    # Something has gone bonkers if there are images in the results that
    # aren't in the request
    extra_images = result_images_set - requested_images_set
    assert len(extra_images) == 0

# ...for each folder


#%% Post-processing (no ground truth)

render_animals_only = False
html_output_files = []

# i_folder = 0; folder_name_raw = folder_names[i_folder]
for i_folder, folder_name_raw in enumerate(folder_names):

    options = PostProcessingOptions()
    options.image_base_dir = read_only_sas_url
    options.parallelize_rendering = True
    options.include_almost_detections = True
    options.num_images_to_sample = 7500
    options.confidence_threshold = 0.8
    options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
    options.ground_truth_json_file = None
    options.separate_detections_by_category = True

    if render_animals_only:
        # Omit some pages from the output, useful when animals are rare
        options.rendering_bypass_sets = ['detections_person','detections_vehicle',
                                          'detections_person_vehicle','non_detections']
    
    folder_name = path_utils.clean_filename(folder_name_raw)
    if len(folder_name) == 0:
        folder_token = ''
    else:
        folder_token = folder_name + '_'
    output_base = os.path.join(postprocessing_output_folder, folder_token + \
        base_task_name + '_{:.3f}'.format(options.confidence_threshold))
    if render_animals_only:
        output_base = output_base + '_animals_only'
    
    os.makedirs(output_base, exist_ok=True)
    print('Processing {} to {}'.format(folder_name, output_base))
    api_output_file = folder_name_to_combined_output_file[folder_name]

    options.api_output_file = api_output_file
    options.output_dir = output_base
    ppresults = process_batch_results(options)
    html_output_files.append(ppresults.output_html_file)

for fn in html_output_files:
    os.startfile(fn)


#%% Manual processing follows

#
# Everything after this should be considered mostly manual, and no longer includes
# looping over folders.
#


#%% Repeat detection elimination, phase 1

# Deliberately leaving these imports here, rather than at the top, because this cell is not
# typically executed
from api.batch_processing.postprocessing.repeat_detection_elimination import repeat_detections_core
import path_utils
task_index = 0

options = repeat_detections_core.RepeatDetectionOptions()

options.confidenceMin = 0.6
options.confidenceMax = 1.01
options.iouThreshold = 0.85
options.occurrenceThreshold = 10
options.maxSuspiciousDetectionSize = 0.2

options.bRenderHtml = False
options.imageBase = read_only_sas_url
rde_string = 'rde_{:.2f}_{:.2f}_{}_{:.2f}'.format(
    options.confidenceMin, options.iouThreshold,
    options.occurrenceThreshold, options.maxSuspiciousDetectionSize)
options.outputBase = os.path.join(filename_base, rde_string + '_task_{}'.format(task_index))
options.filenameReplacements = {'':''}

# Exclude people and vehicles from RDE
# options.excludeClasses = [2,3]

options.debugMaxDir = -1
options.debugMaxRenderDir = -1
options.debugMaxRenderDetection = -1
options.debugMaxRenderInstance = -1

api_output_filename = list(folder_name_to_combined_output_file.values())[task_index]
filtered_output_filename = path_utils.insert_before_extension(api_output_filename, 'filtered_{}'.format(rde_string))

suspiciousDetectionResults = repeat_detections_core.find_repeat_detections(api_output_filename,
                                                                           None,
                                                                           options)

clipboard.copy(os.path.dirname(suspiciousDetectionResults.filterFile))


#%% Manual RDE step

## DELETE THE ANIMALS ##


#%% Re-filtering

from api.batch_processing.postprocessing.repeat_detection_elimination import remove_repeat_detections

remove_repeat_detections.remove_repeat_detections(
    inputFile=api_output_filename,
    outputFile=filtered_output_filename,
    filteringDir=os.path.dirname(suspiciousDetectionResults.filterFile),
    options=options
    )


#%% Post-processing (post-RDE)

render_animals_only = False
html_output_files = []

# i_folder = 0; folder_name_raw = folder_names[i_folder]
for i_folder, folder_name_raw in enumerate(folder_names):

    options = PostProcessingOptions()
    options.image_base_dir = read_only_sas_url
    options.parallelize_rendering = True
    options.include_almost_detections = True
    options.num_images_to_sample = 7500
    options.confidence_threshold = 0.8
    options.almost_detection_confidence_threshold = options.confidence_threshold - 0.05
    options.ground_truth_json_file = None
    options.separate_detections_by_category = True

    if render_animals_only:
        # Omit some pages from the output, useful when animals are rare
        options.rendering_bypass_sets = ['detections_person','detections_vehicle',
                                          'detections_person_vehicle','non_detections']    
    
    folder_name = path_utils.clean_filename(folder_name_raw)
    if len(folder_name) == 0:
        folder_token = ''
    else:
        folder_token = folder_name + '_'
    output_base = os.path.join(postprocessing_output_folder, folder_token + \
        base_task_name + '_{}_{:.3f}'.format(rde_string, options.confidence_threshold))
    if render_animals_only:
        output_base = output_base + '_render_animals_only'
    os.makedirs(output_base, exist_ok=True)
    
    print('Processing {} to {}'.format(folder_name, output_base))
    # api_output_file = folder_name_to_combined_output_file[folder_name]

    options.api_output_file = filtered_output_filename
    options.output_dir = output_base
    ppresults = process_batch_results(options)
    html_output_files.append(ppresults.output_html_file)

for fn in html_output_files:
    os.startfile(fn)


#%% Subsetting

data = None

from api.batch_processing.postprocessing.subset_json_detector_output import (
    subset_json_detector_output, SubsetJsonDetectorOutputOptions)

input_filename = list(folder_name_to_combined_output_file.values())[0]
output_base = os.path.join(filename_base,'json_subsets')

folders = os.listdir(image_base)

if data is None:
    with open(input_filename) as f:
        data = json.load(f)

print('Data set contains {} images'.format(len(data['images'])))

# i_folder = 0; folder_name = folders[i_folder]
for i_folder, folder_name in enumerate(folders):

    output_filename = os.path.join(output_base, folder_name + '.json')
    print('Processing folder {} of {} ({}) to {}'.format(i_folder, len(folders), folder_name,
          output_filename))

    options = SubsetJsonDetectorOutputOptions()
    options.confidence_threshold = 0.4
    options.overwrite_json_files = True
    options.make_folder_relative = True
    options.query = folder_name + '\\'

    subset_data = subset_json_detector_output(input_filename, output_filename, options, data)


#%% String replacement
    
data = None

from api.batch_processing.postprocessing.subset_json_detector_output import (
    subset_json_detector_output, SubsetJsonDetectorOutputOptions)

input_filename = list(folder_name_to_combined_output_file.values())[0]
output_filename = input_filename.replace('.json','_replaced.json')

options = SubsetJsonDetectorOutputOptions()
options.query = folder_name + '/'
options.replacement = ''
subset_json_detector_output(input_filename,output_filename,options)


#%% Folder splitting

from api.batch_processing.postprocessing.separate_detections_into_folders import (
    separate_detections_into_folders, SeparateDetectionsIntoFoldersOptions)

default_threshold = 0.8
base_output_folder = r"e:\organization-out"
base_input_folder = "z:\\"

# i_folder = 0; folder_name_raw = folder_names[i_folder]
for i_folder, folder_name_raw in enumerate(folder_names):

    options = SeparateDetectionsIntoFoldersOptions()
    
    folder_name = path_utils.clean_filename(folder_name_raw)
    api_output_file = folder_name_to_combined_output_file[folder_name]

    options.results_file = api_output_file
    options.base_input_folder = base_input_folder
    options.base_output_folder = os.path.join(base_output_folder,folder_name)
    options.n_threads = 100
    options.allow_existing_directory = False
    
    separate_detections_into_folders(options)

# -*- coding: utf-8 -*-
from dcicutils import ff_utils
from core.pony_utils import (
  FormatExtensionMap,
  get_extra_file_key,
  ProcessedFileMetadata,
  Awsem,
  Tibanna,
  create_ffmeta_awsem
)
from core.utils import powerup
from core.utils import printlog
import boto3
from collections import defaultdict
from core.fastqc_utils import parse_qc_table
import requests
import json

s3 = boto3.resource('s3')


def donothing(status, sbg, ff_meta, ff_key=None):
    return None


def register_to_higlass(tibanna, awsemfile_bucket, awsemfile_key, filetype, datatype):
    payload = {"filepath": awsemfile_bucket + "/" + awsemfile_key,
               "filetype": filetype, "datatype": datatype}
    higlass_keys = tibanna.s3.get_higlass_key()
    if not isinstance(higlass_keys, dict):
        raise Exception("Bad higlass keys found: %s" % higlass_keys)
    auth = (higlass_keys['key'], higlass_keys['secret'])
    headers = {'Content-Type': 'application/json',
               'Accept': 'application/json'}
    res = requests.post(higlass_keys['server'] + '/api/v1/link_tile/',
                        data=json.dumps(payload), auth=auth, headers=headers)
    printlog("LOG resiter_to_higlass(POST request response): " + str(res.json()))
    return res.json()['uuid']


def add_higlass_to_pf(pf, tibanna, awsemfile):
    def register_to_higlass_bucket(key, file_format, file_type):
        return register_to_higlass(tibanna, awsemfile.bucket, key, file_format, file_type)

    if awsemfile.bucket in ff_utils.HIGLASS_BUCKETS:
        higlass_uid = None
        # register mcool/bigwig with fourfront-higlass
        if pf.file_format == "mcool":
            higlass_uid = register_to_higlass_bucket(awsemfile.key, 'cooler', 'matrix')
        elif pf.file_format == "bw":
            higlass_uid = register_to_higlass_bucket(awsemfile.key, 'bigwig', 'vector')
        # bedgraph: register extra bigwig file to higlass (if such extra file exists)
        elif pf.file_format == 'bg':
            for pfextra in pf.extra_files:
                if pfextra.get('file_format') == 'bw':
                    fe_map = FormatExtensionMap(tibanna.ff_keys)
                    extra_file_key = get_extra_file_key('bg', awsemfile.key, 'bw', fe_map)
                    higlass_uid = register_to_higlass_bucket(extra_file_key, 'bigwig', 'vector')
        pf.add_higlass_uid(higlass_uid)


def add_md5_filesize_to_pf(pf, awsemfile):
    if not awsemfile.is_extra:
        pf.status = 'uploaded'
        if awsemfile.md5:
            pf.md5sum = awsemfile.md5
        if awsemfile.filesize:
            pf.file_size = awsemfile.filesize


def add_md5_filesize_to_pf_extra(pf, awsemfile):
    printlog("awsemfile.is_extra=%s" % awsemfile.is_extra)
    if awsemfile.is_extra:
        for pfextra in pf.extra_files:
            if pfextra.get('file_format') == awsemfile.format_if_extra:
                if awsemfile.md5:
                    pfextra['md5sum'] = awsemfile.md5
                if awsemfile.filesize:
                    pfextra['file_size'] = awsemfile.filesize
        printlog("add_md5_filesize_to_pf_extra: %s" % pf.extra_files)


def qc_updater(status, awsemfile, ff_meta, tibanna):
    if ff_meta.awsem_app_name == 'fastqc-0-11-4-1':
        return _qc_updater(status, awsemfile, ff_meta, tibanna,
                           quality_metric='quality_metric_fastqc',
                           file_argument='input_fastq',
                           report_html='fastqc_report.html',
                           datafiles=['summary.txt', 'fastqc_data.txt'])
    elif ff_meta.awsem_app_name == 'pairsqc-single':
        file_argument = 'input_pairs'
        input_accession = str(awsemfile.runner.get_file_accessions(file_argument))
        return _qc_updater(status, awsemfile, ff_meta, tibanna,
                           quality_metric="quality_metric_pairsqc",
                           file_argument=file_argument, report_html='pairsqc_report.html',
                           datafiles=[input_accession + '.summary.out'])
    elif ff_meta.awsem_app_name == 'repliseq-parta':
        return _qc_updater(status, awsemfile, ff_meta, tibanna,
                           quality_metric='quality_metric_dedupqc_repliseq',
                           file_argument='filtered_sorted_deduped_bam',
                           datafiles=['summary.txt'])
    elif ff_meta.awsem_app_name == 'chip-seq-alignment':
        input_accession = str(awsemfile.runner.get_file_accessions('fastqs')[0])
        return _qc_updater(status, awsemfile, ff_meta, tibanna,
                           quality_metric='quality_metric_flagstat_qc',
                           file_argument='bam',
                           datafiles=[input_accession + '.merged.trim_50bp.' + 'flagstat.qc'])


def _qc_updater(status, awsemfile, ff_meta, tibanna, quality_metric='quality_metric_fastqc',
                file_argument='input_fastq', report_html=None,
                datafiles=None):
    # avoid using [] as default argument
    if datafiles is None:
        datafiles = ['summary.txt', 'fastqc_data.txt']
    if status == 'uploading':
        # wait until this bad boy is finished
        return
    # keys
    ff_key = tibanna.ff_keys
    # move files to proper s3 location
    # need to remove sbg from this line
    accession = awsemfile.runner.get_file_accessions(file_argument)[0]
    zipped_report = awsemfile.key
    files_to_parse = datafiles
    if report_html:
        files_to_parse.append(report_html)
    printlog("accession is %s" % accession)
    try:
        files = awsemfile.s3.unzip_s3_to_s3(zipped_report, accession, files_to_parse,
                                            acl='public-read')
    except Exception as e:
        printlog(tibanna.s3.__dict__)
        raise Exception("%s (key={})\n".format(zipped_report) % e)
    # schema. do not need to check_queue
    qc_schema = ff_utils.get_metadata("profiles/" + quality_metric + ".json",
                                      key=ff_key,
                                      ff_env=tibanna.env)
    # parse fastqc metadata
    printlog("files : %s" % str(files))
    filedata = [files[_]['data'] for _ in datafiles]
    if report_html in files:
        qc_url = files[report_html]['s3key']
    else:
        qc_url = None
    meta = parse_qc_table(filedata,
                          qc_schema=qc_schema.get('properties'),
                          url=qc_url)
    printlog("qc meta is %s" % meta)
    # post fastq metadata
    qc_meta = ff_utils.post_metadata(meta, quality_metric, key=ff_key)
    if qc_meta.get('@graph'):
        qc_meta = qc_meta['@graph'][0]
    printlog("qc_meta is %s" % qc_meta)
    # update original file as well
    try:
        original_file = ff_utils.get_metadata(accession,
                                              key=ff_key,
                                              ff_env=tibanna.env,
                                              add_on='frame=object',
                                              check_queue=True)
        printlog("original_file is %s" % original_file)
    except Exception as e:
        raise Exception("Couldn't get metadata for accession {} : ".format(accession) + str(e))
    patch_file = {'quality_metric': qc_meta['@id']}
    try:
        ff_utils.patch_metadata(patch_file, original_file['uuid'], key=ff_key)
    except Exception as e:
        raise Exception("patch_metadata failed in fastqc_updater." + str(e) +
                        "original_file ={}\n".format(str(original_file)))
    # patch the workflow run, value_qc is used to make drawing graphs easier.
    output_files = ff_meta.output_files
    output_files[0]['value_qc'] = qc_meta['@id']
    retval = {"output_quality_metrics": [{"name": quality_metric, "value": qc_meta['@id']}],
              'output_files': output_files}
    printlog("retval is %s" % retval)
    return retval


def get_existing_md5(file_meta):
    md5 = file_meta.get('md5sum', False)
    content_md5 = file_meta.get('content_md5sum', False)
    return md5, content_md5


def which_extra(original_file, format_if_extra=None):
    if format_if_extra:
        if 'extra_files' not in original_file:
            raise Exception("input file has no extra_files," +
                            "yet the tag 'format_if_extra' is found in the input json")
        for extra in original_file.get('extra_files'):
            if extra.get('file_format') == format_if_extra:
                return extra
    return None


def check_mismatch(md5a, md5b):
    if md5a and md5b and md5a != md5b:
        return True
    else:
        return False


def create_patch_content_for_md5(md5, content_md5, original_md5, original_content_md5):
    new_content = {}

    def check_mismatch_and_update(x, original_x, fieldname):
        if check_mismatch(x, original_x):
            raise Exception(fieldname + " not matching the original one")
        if x and not original_x:
            new_content[fieldname] = x
    check_mismatch_and_update(md5, original_md5, 'md5sum')
    check_mismatch_and_update(content_md5, original_content_md5, 'content_md5sum')
    return new_content


def create_extrafile_patch_content_for_md5(new_content, current_extra, original_file):
    current_extra = current_extra.update(new_content.copy())
    return {'extra_files': original_file.get('extra_files')}


def add_status_to_patch_content(content, current_status):
    new_file = content.copy()
    # change status to uploaded only if it is uploading or upload failed
    if current_status in ["uploading", "upload failed"]:
        new_file['status'] = 'uploaded'
    return new_file


def _md5_updater(original_file, md5, content_md5, format_if_extra=None):
    new_file = {}
    current_extra = which_extra(original_file, format_if_extra)
    if current_extra:  # extra file
        original_md5, original_content_md5 = get_existing_md5(current_extra)
        new_content = create_patch_content_for_md5(md5, content_md5, original_md5, original_content_md5)
        if new_content:
            new_file = create_extrafile_patch_content_for_md5(new_content, current_extra, original_file)
    else:
        original_md5, original_content_md5 = get_existing_md5(original_file)
        new_content = create_patch_content_for_md5(md5, content_md5, original_md5, original_content_md5)
        if new_content:
            current_status = original_file.get('status', "uploading")
            new_file = add_status_to_patch_content(new_content, current_status)
    print("new_file = %s" % str(new_file))
    return new_file


def parse_md5_report(read):
    md5_array = read.split('\n')
    if not md5_array:
        raise Exception("md5 report has no content")
    if len(md5_array) == 1:
        md5 = None
        content_md5 = md5_array[0]
    elif len(md5_array) > 1:
        md5 = md5_array[0]
        content_md5 = md5_array[1]
    return md5, content_md5


def md5_updater(status, awsemfile, ff_meta, tibanna):
    # get key
    ff_key = tibanna.ff_keys
    # get metadata about original input file
    accession = awsemfile.runner.get_file_accessions('input_file')[0]
    format_if_extras = awsemfile.runner.get_format_if_extras('input_file')
    original_file = ff_utils.get_metadata(accession,
                                          key=ff_key,
                                          ff_env=tibanna.env,
                                          add_on='frame=object',
                                          check_queue=True)
    if status.lower() == 'uploaded':  # md5 report file is uploaded
        md5, content_md5 = parse_md5_report(awsemfile.read())
        for format_if_extra in format_if_extras:
            new_file = _md5_updater(original_file, md5, content_md5, format_if_extra)
            if new_file:
                break
        printlog("new_file = %s" % str(new_file))
        if new_file:
            try:
                resp = ff_utils.patch_metadata(new_file, accession, key=ff_key)
                printlog(resp)
            except Exception as e:
                # TODO specific excpetion
                # if patch fails try to patch worfklow status as failed
                raise e
    else:
        pass
    # nothing to patch to ff_meta
    return None


def find_pf(pf_meta, accession):
    for pf in pf_meta:
        if pf.accession == accession:
            return pf
    return None


def update_processed_file(awsemfile, pf_meta, tibanna):
    if pf_meta:
        pf = find_pf(pf_meta, awsemfile.accession)
        if not pf:
            raise Exception("Can't find processed file with matching accession: %s" % awsemfile.accession)
        if awsemfile.is_extra:
            try:
                add_md5_filesize_to_pf_extra(pf, awsemfile)
            except Exception as e:
                raise Exception("failed to update processed file metadata %s" % e)
        else:
            try:
                add_higlass_to_pf(pf, tibanna, awsemfile)
            except Exception as e:
                raise Exception("failed to regiter to higlass %s" % e)
            try:
                add_md5_filesize_to_pf(pf, awsemfile)
            except Exception as e:
                raise Exception("failed to update processed file metadata %s" % e)


def update_ffmeta_from_awsemfile(awsemfile, ff_meta, tibanna):
    patch_meta = False
    upload_key = awsemfile.key
    status = awsemfile.status
    printlog("awsemfile res is %s" % status)
    if status == 'COMPLETED':
        patch_meta = OUTFILE_UPDATERS[awsemfile.argument_type]('uploaded', awsemfile, ff_meta, tibanna)
    elif status in ['FAILED']:
        patch_meta = OUTFILE_UPDATERS[awsemfile.argument_type]('upload failed', awsemfile, ff_meta, tibanna)
        ff_meta.run_status = 'error'
        ff_meta.patch(key=tibanna.ff_keys)
        raise Exception("Failed to export file %s" % (upload_key))
    return patch_meta


def update_pfmeta_from_awsemfile(awsemfile, pf_meta, tibanna):
    status = awsemfile.status
    printlog("awsemfile res is %s" % status)
    if status == 'COMPLETED':
        if awsemfile.argument_type == 'Output processed file':
            update_processed_file(awsemfile, pf_meta, tibanna)


def metadata_only(event):
    # just create a fake awsem config so the handler function does it's magic
    '''
    if not event.get('args'):
        event['args'] = {'app_name': event['ff_meta'].get('awsem_app_name'),
                         'output_S3_bucket': 'metadata_only',
                         'output_target': {'metadata_only': 'metadata_only'}
                         }

    if not event.get('config'):
        event['config'] = {'runmode': 'metadata_only'}
    '''

    return real_handler(event, None)


@powerup('update_ffmeta_awsem', metadata_only)
def handler(event, context):
    return real_handler(event, context)


def real_handler(event, context):
    # check the status and other details of import
    '''
    this is to check if the task run is done:
    http://docs.sevenbridges.com/reference#get-task-execution-details
    '''
    # get data
    # used to automatically determine the environment
    tibanna_settings = event.get('_tibanna', {})
    tibanna = Tibanna(tibanna_settings['env'], settings=tibanna_settings)
    ff_meta = create_ffmeta_awsem(
        app_name=event.get('ff_meta').get('awsem_app_name'),
        **event.get('ff_meta')
    )
    pf_meta = [ProcessedFileMetadata(**pf) for pf in event.get('pf_meta')]

    # ensure this bad boy is always initialized
    awsem = Awsem(event)

    # go through this and replace awsemfile_report with awsf format
    # actually interface should be look through ff_meta files and call
    # give me the status of this thing from the runner, and runner.output_files.length
    # so we just build a runner with interface to sbg and awsem
    # runner.output_files.length()
    # runner.output_files.file.status
    # runner.output_files.file.loc
    # runner.output_files.file.get

    if event.get('error', False):
        ff_meta.run_status = 'error'
        ff_meta.description = event.get('error')
        ff_meta.patch(key=tibanna.ff_keys)
        raise Exception(event.get('error'))

    metadata_only = event.get('metadata_only', False)

    awsem_output = awsem.output_files()
    awsem_output_extra = awsem.secondary_output_files()
    ff_output = len(ff_meta.output_files)
    if len(awsem_output) != ff_output:
        ff_meta.run_status = 'error'
        ff_meta.description = "%d files output expected %s" % (ff_output, len(awsem_output))
        ff_meta.patch(key=tibanna.ff_keys)
        raise Exception("Failing the workflow because outputed files = %d and ffmeta = %d" %
                        (awsem_output, ff_output))

    def update_metadata_from_awsemfile_list(awsemfile_list):
        patch_meta = False
        for awsemfile in awsemfile_list:
            patch_meta = update_ffmeta_from_awsemfile(awsemfile, ff_meta, tibanna)
            if not metadata_only:
                update_pfmeta_from_awsemfile(awsemfile, pf_meta, tibanna)
        # allow for a simple way for updater to add appropriate meta_data
        if patch_meta:
            ff_meta.__dict__.update(patch_meta)

    update_metadata_from_awsemfile_list(awsem_output)
    update_metadata_from_awsemfile_list(awsem_output_extra)

    # if we got all the awsemfiles let's go ahead and update our ff_metadata object
    ff_meta.run_status = "complete"

    # add postrunjson log file to ff_meta as a url
    ff_meta.awsem_postrun_json = get_postrunjson_url(event)

    # make all the file awsemfile meta-data stuff here
    # TODO: fix bugs with ff_meta mapping for output and input file
    try:
        ff_meta.patch(key=tibanna.ff_keys)
    except Exception as e:
        raise Exception("Failed to update run_status %s" % str(e))
    # patch processed files - update only status, extra_files, md5sum and file_size
    if pf_meta:
        patch_fields = ['uuid', 'status', 'extra_files', 'md5sum', 'file_size']
        try:
            for pf in pf_meta:
                printlog(pf.as_dict())
                pf.patch(key=tibanna.ff_keys, fields=patch_fields)
        except Exception as e:
            raise Exception("Failed to update processed metadata %s" % str(e))

    event['ff_meta'] = ff_meta.as_dict()
    event['pf_meta'] = [_.as_dict() for _ in pf_meta]

    return event


def get_postrunjson_url(event):
    try:
        logbucket = event['config']['log_bucket']
        jobid = event['jobid']
        postrunjson_url = 'https://s3.amazonaws.com/' + logbucket + '/' + jobid + '.postrun.json'
        return postrunjson_url
    except Exception as e:
        # we don't need this for pseudo runs so just ignore
        if event.get('metadata_only'):
            return ''
        else:
            raise e


# Cardinal knowledge of all workflow updaters
OUTFILE_UPDATERS = defaultdict(lambda: donothing)
OUTFILE_UPDATERS['Output report file'] = md5_updater
OUTFILE_UPDATERS['Output QC file'] = qc_updater

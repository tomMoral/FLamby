from sklearn.model_selection import train_test_split
from tciaclient import TCIAClient
import pandas as pd
import os
import tqdm
from joblib import Parallel, delayed
import dask.dataframe as dd
import glob
import requests
from process_raw import convert_to_niftis
import multiprocessing
from pathlib import Path
import zipfile
import re
import xml.etree.ElementTree as ET
try:
    n_cpus = multiprocessing.cpu_count()
except NotImplementedError:
    n_cpus = 5   # arbitrary default


BASEURL = "https://services.cancerimagingarchive.net/services/v4"
RESSOURCE = "TCIA"
# Found on the LIDC-IDRI webpage (https://wiki.cancerimagingarchive.net/display/Public/LIDC-IDRI)
ANNOTATION_URL = 'https://wiki.cancerimagingarchive.net/download/attachments/1966254/LIDC-XML-only.zip?version=1&modificationDate=1530215018015&api=v2'
SERVER_URL = BASEURL + '/query/getImage?SeriesInstanceUID='
DOWNLOAD_DIR = os.path.expanduser("~/Desktop/LIDC-dataset")
client = TCIAClient(BASEURL, RESSOURCE)


def download_dicom_series(uid):
    """Use the TCIA client to retrieve a zip of DICOMS associated with a uid
    Parameters
    ----------
    uid : str
        The uid of a series
    Returns
    -------
    str
        The filename minus the zip extension
    """
    filename_zipped = os.path.join(DOWNLOAD_DIR, uid + ".zip")
    filename = re.sub(".zip", "", filename_zipped)
    if not(os.path.exists(filename_zipped) or os.path.isdir(filename)):
        client.get_image(seriesInstanceUid=uid, downloadPath=DOWNLOAD_DIR, zipFileName=uid + ".zip")
    return filename


def download_zip_from_url(url, download_dir="."):
    """Downloads a zip file from a link in the download_dir folder
    Parameters
    ----------
    url : str
        The url link of the file to download
    download_dir : str, optional
        The folder in which to store the zip file, by default "."
    Returns
    -------
    str
        The filename minus the zip extension
    """

    filename = url.split("/")[-1].split("?")[0].strip("\\")
    os.makedirs(download_dir, exist_ok=True)
    filename_zipped = os.path.join(download_dir, filename)
    filename = re.sub(".zip", "", filename_zipped)
    if not(os.path.exists(filename_zipped) or os.path.isdir(filename)):
        print("downloading: ", url)
        r = requests.get(url, stream=True)
        if r.status_code == requests.codes.ok:
            with open(filename_zipped, 'wb') as f:
                for data in r:
                    f.write(data)
    return filename


def get_SeriesUID_from_xml(path):
    """Retrieves SeriesUID from the xml under scrutiny.
    Parameters
    ----------
    path : str
        The path towards a valid XML file
    Returns
    -------
    str
        Either the Series UID included the XML or the not found string
    """
    try:
        return [e.text for e in ET.parse(path).getroot().iter() if e.tag == '{http://www.nih.gov}SeriesInstanceUid'][0]
    except Exception as ex:
        return 'notfound'


def download_LIDC():
    """Download the LIDC dataset in the DOWNLOAD_DIR folder and link downloaded DICOMs with annotation files.
    Returns
    -------
    pd.DataFrame
        A dataframe with all informations regarding the raw data.
    """
    # Get patient X study
    patientXstudy = pd.io.json.read_json(
        client.get_patient_study(collection='LIDC-IDRI').read())

    # Get study X series
    series = pd.io.json.read_json(client.get_series(
        modality='CT', collection='LIDC-IDRI').read())

    # Join both of them
    patientXseries = patientXstudy.merge(series).iloc[:]

    # Download associated DICOMs
    pool = multiprocessing.Pool(processes=n_cpus)
    downloaded_paths = pool.map(download_dicom_series, patientXseries.SeriesInstanceUID.tolist())

    # Download XML annotations
    annotations_path = download_zip_from_url(ANNOTATION_URL, DOWNLOAD_DIR)

    # Unzip everything and remove archives
    zipped_folders = [str(p) for p in Path(DOWNLOAD_DIR).glob('./*/') if str(p).endswith(".zip")]

    for zipped_f in zipped_folders:
        with zipfile.ZipFile(zipped_f, 'r') as zip_ref:
            try:
                os.makedirs(re.sub(".zip", "", zipped_f), exist_ok=False)
                zip_ref.extractall(re.sub(".zip", "", zipped_f))
            except FileExistsError:
                pass
        os.remove(zipped_f)

    # For each patient we record the location of its DICOM
    patientXseries['extraction_location'] = downloaded_paths

    # We tie back annotations to the original DICOMS
    xmlfiles = glob.glob(os.path.join(annotations_path, 'tcia-lidc-xml', '*', '*.xml'))
    df = pd.DataFrame()
    df['annotation_file'] = xmlfiles
    # We initialize a dask dataframe to speed up computations
    ddf = dd.from_pandas(df, npartitions=8)
    df['SeriesInstanceUID'] = ddf.map_partitions(lambda d: d['annotation_file'].apply(get_SeriesUID_from_xml)).compute(scheduler='processes')
    df = df[df.SeriesInstanceUID != 'not found']
    patientXseries = df.merge(patientXseries, on="SeriesInstanceUID")

    # # We add the path towards the global annotation file as the last patient
    # patientXseries = patientXseries.append({'PatientID': 'annotations', 'extraction_location': re.sub(".zip", "", annotations_path)}, ignore_index=True)

    return patientXseries


def LIDC_to_niftis(extraction_results_dataframe, spacing=[1., 1., 1.]):
    """Turns the raw dataset to nifti formats
    Parameters
    ----------
    extraction_results_dataframe : pd.Dataframe
        Dataframe
    spacing : list, optional
        The spacing to use for the nifti conversion
    Returns
    -------
    pd.DataFrame
        The dataframe of the data that could be successfully converted to nifti formats.
    """

    loop = map(lambda t: t[1][['extraction_location', 'annotation_file']].values, extraction_results_dataframe.iterrows())

    progbar = tqdm.tqdm(
        loop, total=extraction_results_dataframe.shape[0], desc='Converting to NiFTIs...')

    converted_dicoms = Parallel(n_jobs=1, prefer='processes')(
        delayed(convert_to_niftis)(*t, spacing=spacing) for t in progbar)

    initial_shape = extraction_results_dataframe.shape[0]
    extraction_results_dataframe = extraction_results_dataframe[converted_dicoms]
    final_shape = extraction_results_dataframe.shape[0]
    print(f'{final_shape}/{initial_shape} DICOMs folders successfully converted.')

    return extraction_results_dataframe
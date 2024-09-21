import numpy as np
import pandas as pd
import scanpy as sc
import squidpy as sq
from skimage import io
from typing import Tuple, Optional
from skimage import filters
import anndata
import scipy
import math
import cv2
import warnings

class STData:
    """
    A class for handling and managing spatial transcriptomics data.

    This class processes and stores various components of spatial transcriptomics data,
    including image data, spot coordinates, and gene expression data.

    Parameters
    ----------
    adata : anndata.AnnData
        AnnData object containing the spatial transcriptomics data.
    section_name : str
        Name of the tissue section.
    scale_factor : float
        Scale factor for adjusting coordinates and image size.
    radius : float
        Radius of spots in original scale.
    swap_coord : bool
        Whether to swap the x and y coordinates. Defaults to True.
    create_mask : bool
        Whether to create a mask for the image. Defaults to True.
    """

    def __init__(self,
                 adata: anndata.AnnData,
                 section_name: str,
                 scale_factor: float,
                 radius: float,
                 swap_coord: bool = True,
                 create_mask: bool = True):

        # Process the AnnData object
        self.adata = preprocess_adata(adata, swap_coord)

        # Extract image and spot coordinates
        image = adata.uns['spatial'][list(adata.uns['spatial'].keys())[0]]['images']['orires'].copy()
        spot_coord = adata.obsm['spatial']

        del adata.uns['spatial'][list(adata.uns['spatial'].keys())[0]]['images']['orires']

        # Initialize the STData object
        self.section_name = section_name
        self.scale_factor = scale_factor
        self.radius = int(radius / scale_factor)
        self.kernel_size = self.radius // 2 * 2 + 1

        # Initialize the scores and save_paths dictionaries
        self.save_paths = None
        self.scores = {'NMF': None, 'GCN': None, 'VD': None, 'SpaHDmap': None}
        self.tissue_coord = None

        # Preprocess the image and spot expression data
        self._preprocess(spot_coord, image, create_mask)

    @property
    def spot_exp(self):
        return self.adata.X.toarray() if isinstance(self.adata.X, scipy.sparse.spmatrix) else self.adata.X

    @property
    def genes(self):
        return self.adata.var_names.tolist()

    def __repr__(self):
        """
        Return a string representation of the STData object.
        """
        return (f"STData object for section: {self.section_name}\n"
                f"Number of spots: {self.num_spots}\n"
                f"Number of genes: {len(self.genes)}\n"
                f"Image shape: {self.image.shape}\n"
                f"Scale factor: {self.scale_factor}\n"
                f"Spot radius: {self.radius}\n"
                f"Image type: {self.image_type}\n"
                f"Available scores: {', '.join(score for score, value in self.scores.items() if value is not None)}")

    def __str__(self):
        """
        Return a string with a summary of the STData object.
        """
        return (f"STData object for section: {self.section_name}\n"
                f"Number of spots: {self.num_spots}\n"
                f"Number of genes: {len(self.genes)}\n"
                f"Image shape: {self.image.shape}\n"
                f"Scale factor: {self.scale_factor}\n"
                f"Spot radius: {self.radius}\n"
                f"Image type: {self.image_type}\n"
                f"Available scores: {', '.join(score for score, value in self.scores.items() if value is not None)}")

    def _preprocess(self,
                     spot_coord: np.ndarray,
                     image: np.ndarray,
                     create_mask: bool):
        """
        Preprocess spot_coord and prepare the feasible domain and process the image.

        Parameters
        ----------
        spot_coord : np.ndarray
            Array of original spot coordinates.
        image : np.ndarray
            Original image data.
        create_mask : bool
            Whether to create a mask for the image.
        """

        # Process the spot coordinates
        self.spot_coord = spot_coord / self.scale_factor - 1
        self.num_spots = self.spot_coord.shape[0]
        min_coords, max_coords = self.spot_coord.min(0).astype(int), self.spot_coord.max(0).astype(int)
        tmp_row_range = (max(0, min_coords[0] - self.radius), min(image.shape[0], max_coords[0] + self.radius + 1))
        tmp_col_range = (max(0, min_coords[1] - self.radius), min(image.shape[1], max_coords[1] + self.radius + 1))

        # Process the image
        image = image / np.max(image, axis=(0, 1), keepdims=True)

        hires_shape = (math.ceil(image.shape[0] / self.scale_factor), math.ceil(image.shape[1] / self.scale_factor))
        lowres_shape = (math.ceil(image.shape[0] / 16), math.ceil(image.shape[1] / 16))

        hires_image = cv2.resize(image, (hires_shape[1], hires_shape[0]), interpolation=cv2.INTER_AREA).astype(np.float32)
        lowres_image = cv2.resize(image, (lowres_shape[1], lowres_shape[0]), interpolation=cv2.INTER_AREA).astype(np.float32)

        self.image_type = _classify_image_type(lowres_image)
        print(f"Processing image, seems to be {self.image_type} image.")

        self.image = np.transpose(hires_image, (2, 0, 1))

        if create_mask:
            # Create masks for outer regions
            gray = cv2.cvtColor(lowres_image, cv2.COLOR_RGB2GRAY)

            ## Apply Otsu's thresholding
            thresh = filters.threshold_otsu(gray)
            binary_mask = gray > thresh

            outer_mask = np.ones(lowres_shape, dtype=np.bool_)
            outer_mask[tmp_row_range[0]//16:tmp_row_range[1]//16, tmp_col_range[0]//16:tmp_col_range[1]//16] = 0

            ## Calculate variance for both classes in the outer region
            outer_pixels = gray[outer_mask]
            var_background = np.var(outer_pixels[binary_mask[outer_mask]])
            var_foreground = np.var(outer_pixels[~binary_mask[outer_mask]])

            ## Determine which class is the background based on lower variance and calculate background value
            background_pixels = lowres_image[outer_mask & binary_mask] if var_background < var_foreground else lowres_image[outer_mask & ~binary_mask]
            background_value = np.median(background_pixels, axis=0)

            # Create mask of image
            mask, tmp_mask = np.zeros(hires_shape, dtype=np.bool_), np.zeros(hires_shape, dtype=np.bool_)
            mask[np.where(np.mean(np.abs(hires_image - background_value[None, None, :]), axis=2) > 0.075)] = 1

            ## Overlap mask with spot coordinates
            tmp_mask[tmp_row_range[0]:tmp_row_range[1], tmp_col_range[0]:tmp_col_range[1]] = 1
            mask = np.logical_and(mask, tmp_mask)

            ## Close and open the mask
            if self.image_type == 'Protein':
                mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((self.radius*4, self.radius*4), np.uint8)).astype(np.bool_)
            else:
                mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((self.radius//4, self.radius//4), np.uint8)).astype(np.bool_)

            mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((self.radius//2, self.radius//2), np.uint8)).astype(np.bool_)

            ## Get row and column ranges and final mask
            mask_idx = np.where(mask == 1)
            self.row_range = (np.min(mask_idx[0]), np.max(mask_idx[0]))
            self.col_range = (np.min(mask_idx[1]), np.max(mask_idx[1]))
            self.mask = mask[self.row_range[0]:self.row_range[1], self.col_range[0]:self.col_range[1]]

        else:
            self.mask = mask = np.ones(hires_shape, dtype=np.bool_)
            self.row_range = (0, hires_shape[0])
            self.col_range = (0, hires_shape[1])

        # Create feasible domain
        self.feasible_domain = mask.copy()
        for (row, col) in self.spot_coord:
            row, col = round(row), round(col)
            row_range = np.arange(max(row - self.radius, 0), min(row + self.radius + 1, hires_shape[0]))
            col_range = np.arange(max(col - self.radius, 0), min(col + self.radius + 1, hires_shape[1]))
            self.feasible_domain[np.ix_(row_range, col_range)] = 0

def _classify_image_type(image):
    """
    Classify an image as either H&E stained or high dynamic range protein fluorescence.

    Parameters
    ----------
    image : numpy.ndarray
        The input image. Can be high bit depth.

    Returns
    -------
    str
        'HE' for H&E stained images, 'Protein' for protein fluorescence images.
    """

    # Calculate histogram
    hist, bin_edges = np.histogram(image.flatten(), bins=1000, range=(0, 1))

    # Calculate metrics
    low_intensity_ratio = np.sum(hist[:100]) / np.sum(hist)
    high_intensity_ratio = np.sum(hist[-100:]) / np.sum(hist)

    # Check for characteristics of protein fluorescence images
    if low_intensity_ratio > 0.5 and high_intensity_ratio < 0.05:
        return 'Protein'
    return 'HE'

def read_10x_data(data_path: str) -> anndata.AnnData:
    """
    Read 10x Visium spatial transcriptomics data.

    Parameters
    ----------
    data_path : str
        Path to the 10x Visium data directory.

    Returns
    -------
    anndata.AnnData
        AnnData object containing the spatial transcriptomics data.
    """
    adata = sc.read_visium(data_path)
    return adata

def read_from_image_and_coord(image_path: str,
                              coord_path: str,
                              exp_path: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Read data from separate image, coordinate, and expression files.

    Parameters
    ----------
    image_path : str
        Path to the H&E image file.
    coord_path : str
        Path to the spot coordinates file.
    exp_path : str
        Path to the gene expression file.

    Returns
    -------
    Tuple
        Tuple containing:
            - image (np.ndarray): H&E image data.
            - spot_coord (np.ndarray): Spot coordinates.
            - spot_exp (np.ndarray): Gene expression data.
    """
    # Read image
    image = io.imread(image_path)

    # Read spot coordinates
    spot_coord = pd.read_csv(coord_path, index_col=0).values

    # Read gene expression data
    spot_exp = pd.read_csv(exp_path, index_col=0).values

    return image, spot_coord, spot_exp

def preprocess_adata(adata: anndata.AnnData,
                     swap_coord: bool = True) -> anndata.AnnData:
    """
    Preprocess the spatial transcriptomics data, including normalization and SVG selection using squidpy.

    Parameters
    ----------
    adata : anndata.AnnData
        AnnData object containing the spatial transcriptomics data.
    swap_coord : bool
        Whether to swap the x and y coordinates. Defaults to True.

    Returns
    -------
    anndata.AnnData
        Preprocessed AnnData object.
    """

    print(f"Pre-processing gene expression data for {adata.shape[0]} spots and {adata.shape[1]} genes.")

    # Swap x and y coordinates
    if swap_coord:
        adata.obsm['spatial'] = adata.obsm['spatial'][:, ::-1]
        print("Swapping x and y coordinates.")
    else:
        warnings.warn("Coordinates are not swapped. Make sure the coordinates are in the correct order.")

    # Normalize data
    if adata.X.max() < 20:
        warnings.warn("Data seems to be already normalized, skipping normalization.")
    else:
        adata.var_names_make_unique()
        sc.pp.filter_cells(adata, min_genes=3)
        sc.pp.filter_genes(adata, min_cells=3)
        sc.pp.normalize_total(adata, target_sum=1e4)
        sc.pp.log1p(adata)
        sc.pp.highly_variable_genes(adata, flavor="seurat", n_top_genes=10000)
        adata = adata[:, adata.var.highly_variable]

    return adata

def prepare_stdata(section_name: str,
                   image_path: str,
                   adata: Optional[sc.AnnData] = None,
                   scale_factor: float = 1,
                   radius: float = None,
                   swap_coord: bool = True,
                   create_mask: bool = True,
                   **kwargs):
    """
    Prepare STData object from various data sources with priority.

    Parameters
    ----------
    section_name : str
        Name of the tissue section.
    image_path : str
        Path to the H&E image file. This is a required parameter.
    adata : Optional[sc.AnnData]
        AnnData object containing the spatial transcriptomics data.
    scale_factor : float
        Scale factor for adjusting coordinates and image size. Defaults to 1.
    radius : float
        Radius of spots in original scale. Defaults to None.
    swap_coord : bool
        Whether to swap the x and y coordinates. Defaults to True.
    create_mask : bool
        Whether to create a mask for the image. Defaults to True.
    **kwargs : Additional keyword arguments, including visium_path, spot_coord_path, and spot_exp_path.
    visium_path : str
        Path to the 10x Visium data directory.
    spot_coord_path : str
        Path to the spot coordinates file.
    spot_exp_path : str
        Path to the gene expression file.

    Returns
    -------
    STData
        Prepared STData object.
    """

    # Check for AnnData
    if adata is not None:
        print(f"*** Reading and preparing AnnData for section {section_name} ***")

    # Check for 10 Visium data if AnnData is not available
    elif 'visium_path' in kwargs and kwargs['visium_path'] is not None:
        print(f"*** Reading and preparing Visium data for section {section_name} ***")
        adata = sc.read_visium(kwargs['visium_path'])

    # Read from scratch if neither AnnData nor 10 Visium is available
    else:
        print(f"*** Reading and preparing data from scratch for section {section_name} ***")
        spot_coord_path = kwargs.get('spot_coord_path')
        spot_exp_path = kwargs.get('spot_exp_path')

        if not all([spot_coord_path, spot_exp_path]):
            raise ValueError("Missing required paths for reading from scratch.")

        adata = sc.read(spot_exp_path)

        spot_coord = pd.read_csv(spot_coord_path, index_col=0)
        spot_coord[['x_coord', 'y_coord']] = spot_coord.iloc[:, :2]
        spot_coord = spot_coord[['x_coord', 'y_coord']]

        # Add spot coordinates and image to adata
        adata.obsm['spatial'] = spot_coord.loc[adata.obs_names].values

    image = io.imread(image_path)
    if 'spatial' not in adata.uns:
        adata.uns['spatial'] = {section_name: {'images': {'orires': image}}}

    else:
        section_id = list(adata.uns['spatial'].keys())[0]
        try:
            radius = round(adata.uns['spatial'][section_id]['scalefactors']['spot_diameter_fullres'] / 2)
            print(f"Spot radius found in AnnData: {radius}")
        except KeyError:
            if radius is not None:
                warnings.warn("Radius is specified but not found in AnnData. Using the specified radius instead.")
        adata.uns['spatial'][section_id]['images']['orires'] = image

    if radius is None:
        warnings.warn("Radius is not found, using default radius of 65.")
        radius = 65

    # Create STData object
    st_data = STData(adata,
                     section_name=section_name,
                     scale_factor=scale_factor,
                     radius=radius,
                     swap_coord=swap_coord,
                     create_mask=create_mask)
    return st_data

def select_svgs(section: STData | list[STData],
                n_top_genes: int = 3000):
    """
    Select the top SVGs based on Moran's I across multiple tissue sections.
    Update each section's AnnData object to only include the selected SVGs.

    Parameters
    ----------
    section : STData | list[STData]
        STData object or list of STData objects.
    n_top_genes : int
        Number of top SVGs to select.

    """
    sections = section if isinstance(section, list) else [section]

    # Find the overlap of genes across all sections
    overlap_genes = set(sections[0].genes)
    for section in sections:
        overlap_genes = overlap_genes.intersection(section.genes)
        # Compute spatial neighbors
        sq.gr.spatial_neighbors(section.adata)
    overlap_genes = list(overlap_genes)

    if len(sections) > 1:
        print(f"Find {len(overlap_genes)} overlapping genes across {len(sections)} sections.")

    # If the number of overlapping genes is less than or equal to n_top_genes, select all of them
    if len(overlap_genes) <= n_top_genes:
        warnings.warn(
            "Number of genes is less than the specified number of top genes, using all genes.")
        selected_genes = overlap_genes
    else:
        # Compute Moran's I for overlapping genes across all sections
        moran_i_values = []
        for section in sections:
            sq.gr.spatial_autocorr(section.adata, mode="moran", genes=overlap_genes)
            moran_i_values.append(section.adata.uns['moranI']['I'])

        # Combine Moran's Index results and select top n_top_genes
        combined_moran_i = pd.concat(moran_i_values, axis=1, keys=[s.section_name for s in sections])
        combined_moran_i['mean_rank'] = combined_moran_i.mean(axis=1).rank(method='dense', ascending=False)
        selected_genes = combined_moran_i.sort_values('mean_rank').head(n_top_genes).index.tolist()

    # Update each section's AnnData object with the selected SVGs
    for section in sections:
        section.adata = section.adata[:, selected_genes]

    print(f"Selected {len(selected_genes)} SVGs.")
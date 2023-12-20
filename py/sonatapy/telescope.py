'''
Superclass with most of the general code for the reduction pipelines
'''

import os, glob
from copy import deepcopy

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

from scipy.signal import find_peaks

from rascal.calibrator import Calibrator
from rascal.atlas import Atlas
from rascal.util import refine_peaks

from astropy.io import fits
import astropy.units as u
from astropy.nddata import CCDData
from astropy.modeling.polynomial import Polynomial1D
from astropy.modeling.fitting import LinearLSQFitter
from astropy.modeling.models import Gaussian1D
from astropy.modeling.fitting import LevMarLSQFitter
import ccdproc

class Telescope(object):

    def __init__(self, datadir:str, obj_name:str, standard_name:str, debug:bool=False):
        '''
        A telescope that SONATA uses and needs to reduce data from. This should be subclassed
        with the specifics of the telescope. See bok.py for an example!
    
        Args:
            datadir [str]: The path to the directory with the data organized with the 
                           following subdirectories:
                           1) obj_name (provided as an argument here)
                           2) standard_name (provided as an argument here)
                           3) flats
                           4) zero
                           The obj_name subdirectory should have an obj_name_arc.fits
                           as the arc file for that object
            obj_name [str]: The name of the object subdirectory to reduce
            standard_name [str]: The name of the standard star subdirectory
        '''

        # define some useful paths
        # these do not need to be instance variables because once we read the data
        # we don't care about where it was exactly
        science = glob.glob(os.path.join(datadir, obj_name, '*.fits')) # science image files
        flats = glob.glob(os.path.join(datadir, 'flats', '*.fits')) # flat image files
        standard = glob.glob(os.path.join(datadir, 'feige', '*.fits')) # standard star image files
        zeros = glob.glob(os.path.join(datadir, 'zero', '*.fits')) # Zero images

        if not all([os.path.exists(p) for p in science]):
            raise ValueError(f'{obj_name} not in {datadir}!')

        print(science)
        
        self.spectra2d = self._read_imgs(science[1:], debug=debug)
        self.arc2d = self._read_imgs([science[0]], debug=debug)
        self.flat2d = self._read_imgs(flats, debug=debug)
        self.zero2d = self._read_imgs(zeros, debug=debug)
        self.stan2d = self._read_imgs(standard, debug=debug)

        # reduce the spectra using the flats and zeros
        # this shouldn't change between telescopes
        self._process()  
        
    def _read_imgs(self, fitslist, method='average', debug=False, min_trim=40,
                  max_trim=1200, **kwargs
                  ):
        '''
        fitslist [list[str]]: List of fits files to read in
        method [str]: the method to stack the images by
        debug [bool]: if true plot some debug plots
        **kwargs: Any other arguments to pass to ccdproc.combine
        '''

        spectra2d = ccdproc.combine(fitslist, method=method, unit=u.count, sigma_clip=True, 
                                    sigma_clip_low_thresh=5, sigma_clip_high_thresh=5,
                                    sigma_clip_func=np.ma.median, mem_limit=350e6
                                    )
        spectra2d = ccdproc.trim_image(spectra2d[min_trim:, :max_trim])

        if debug:
            fig, ax = plt.subplots(figsize=(18,6))
            ax.imshow(spectra2d)
            fig.savefig('2d_spectrum.png')

        return spectra2d

    def _process(self):
        '''
        Processes the image and combines it with the zeris and flats
        '''

        # the science image
        self.science_red = ccdproc.ccd_process(self.spectra2d, 
                                       master_bias=self.zero2d,
                                       master_flat=self.flat2d)

        # the arc
        self.arc_red = ccdproc.ccd_process(self.arc2d, 
                                       master_bias=self.zero2d,
                                       master_flat=self.flat2d)


        # the standard star
        self.stan_red = ccdproc.ccd_process(self.stan2d, 
                                       master_bias=self.zero2d,
                                       master_flat=self.flat2d)

    @staticmethod
    def compute_light_frac(img, center, width):
        '''
        Computes the fraction of light within the width around the center
        '''
        top = (center+width).astype(int)
        bot = (center-width).astype(int)

        arr = np.array([img[int(val)-width:int(val)+width,ii] for ii, val in enumerate(center)])

        region = np.sum(arr)
        tot = np.sum(img)

        return region/tot

    @classmethod
    def extract_trace_profile(cls, spec, polyfit, center, max_frac=0.1, width_init=1,
                              debug=False, **extras):
        '''
        Dynamically extract a trace profile from the reduced 2d spectrum

        This function varies the width of the trace profile to get the extracted light fraction
        to be >= the specified maximum light fraction

        Args:
            spec [np.ndarray]: 2D spectrum, reduced
            polyfit [function]: A callable function with the model for the 2D spectrum center
            max_frac [float]: Float between 0 and 1 for how much light should be inside
                              the spectral region. Larger means wider region but higher
                              possibility of getting background signal.
            width_init [int]: Initial width of the spectral extraction region. Default is 1.
            debug [bool]: will generate debug plots if True
        '''

        light_frac_init = cls.compute_light_frac(spec, center, width_init)

        dwidth = 1
        light_frac = light_frac_init
        width = width_init

        while light_frac < max_frac:

            new_light_frac = cls.compute_light_frac(spec, center, width)
            if new_light_frac > light_frac:
                light_frac = new_light_frac

            width += dwidth

        return width

    @classmethod
    def trace_spectrum(cls, spec, arc=False, background_subtract=True,
                       debug=False, **extras):
        '''
        Extract a trace of the 2d spectrum
        '''

        # first estimate the trace values using argmax
        # this should give an estimate of factor and center
        if arc:
            # for an arc we can just take a slice down the center
            center_idx = spec.shape[0]//2
            yvals = np.ones(spec.shape[1])*center_idx  # draw the line down the center
        else:
            yvals = np.argmax(spec, axis=0)
        
        xvals = np.arange(spec.shape[1])
        polymodel = Polynomial1D(degree=3)
        linfitter = LinearLSQFitter()
        est_fit = linfitter(polymodel, xvals, yvals)
        est_center = est_fit(xvals)
        est_width = cls.extract_trace_profile(spec, est_fit, est_center)

        # then background subtract
        # estimate the most common value
        factor = est_width
        
        yhist = np.histogram(yvals, len(yvals)//factor)
        ycomm_idx = yhist[0].argmax()
        ycomm = int(yhist[1][ycomm_idx])

        yaxis = np.repeat(np.arange(ycomm-factor, ycomm+factor)[:,None],
                          spec.shape[1], axis=1)

        # one way to define background
        #background=np.median(spectra2d)
        
        # arguably a better way to define background
        # since this finds the average value of the background signal
        if background_subtract:
            background = deepcopy(spec)
            background[ycomm-factor:ycomm+factor, :] = 0 # set all of these to no background signal
            background=np.mean(spec, axis=0)
        else:
            background = np.zeros(spec.shape[1])

        if debug:
            plt.figure(figsize=(18,6))
            bckgd_toplot = spec.shape[0]*background/max(background)
            plt.plot(xvals, bckgd_toplot, 'w:', alpha=0.5)
            plt.imshow(spec, extent=[0,spec.shape[1],0,spec.shape[0]], 
                       origin='lower')
            plt.gca().set_aspect(20)
            #plt.ylim(0, spec.shape[0])
            plt.title('Background Signal 1D')

        # take the first moment
        weights = spec[ycomm-factor:ycomm+factor, :]-background
        where0 = np.where(np.sum(weights, axis=0) == 0)[0]
        if len(where0) > 0:
            weights[:, where0] = np.ones(weights[:, where0].shape)
        plt.imshow(weights)
        plt.gca().set_aspect(20)

        yvals = np.average(yaxis, axis=0,
                          weights=weights)

        if debug:
            plt.figure()
            plt.imshow(spec[ycomm-factor:ycomm+factor, :]-background)
            plt.title('Background Subtracted 2D Spectrum')
            plt.gca().set_aspect(20)

        # remove outliers
        nsigma = 5
        med = np.mean(yvals)
        q = np.std(yvals)
        whereNotOutlier = np.where((yvals < (med+nsigma*q)) * (yvals > (med-nsigma*q)))[0] 

        xmax = xvals[whereNotOutlier]
        ymax = yvals[whereNotOutlier]

        # fit with a polynomial
        polymodel = Polynomial1D(degree=3)
        linfitter = LinearLSQFitter()
        fitted_polymodel = linfitter(polymodel, xmax, ymax)

        if debug:
            plt.figure(figsize=(6,18))
            minval = 38
            maxval = 58
            plt.imshow(spec[minval:maxval, :], extent=[0,spec.shape[1],minval,maxval], 
                       norm=LogNorm(), cmap='Greys', origin='lower')
            plt.plot(xmax, ymax, 'x')
            plt.gca().set_aspect(20)
            plt.xlabel("X Coordinate")
            plt.ylabel("Moment-1 estimated Y-value trace");
            plt.plot(xmax, fitted_polymodel(xmax), color='r');
            plt.title('Best Trace Fit')

        return xvals, yvals, fitted_polymodel, background
    
    @classmethod
    def extract_1d(cls, spec, arc=False, background_subtract=True, debug=False, **kwargs):
        '''
        Extract the 1d spectrum from the 2d spectrum
        '''    
        # first we fit the 2D spectrum using the moments method
        xvals, yvals, poly, bckgd = cls.trace_spectrum(spec, arc=arc,
                                                       background_subtract=background_subtract,
                                                       debug=debug,
                                                       **kwargs)
        center = poly(xvals)

        # then we extract the trace profile
        width = cls.extract_trace_profile(spec, poly, center, debug=debug, **kwargs)

        print(len(xvals))
        # then extract the 1d spectrum
        print(spec[int(center[0])-width:int(center[0])+width, :].shape)
        cutout = np.array([spec[int(val)-width:int(val)+width, ii] for ii, val in enumerate(center)]).T
        spectrum1d = (cutout-bckgd).mean(axis=0)
        if debug:
            plt.figure()
            plt.plot(spectrum1d)
            plt.title('Final 1D Spectrum')

        return spectrum1d

    @classmethod
    def wavelength_solver(cls, arc, element_list, min_wave, max_wave,
                          tol=100, nbins=200, max_tries=500,
                          line_brightness=0.001, dist=100, debug=False, **extras):
        '''
        Solve for the wavelengths

        NOTE: Try tuning line_brightness and dist if the fit doesn't make sense! I've
              found that what I have put as defaults are very strict and only use very
              strong lines.

        Args:
            arc [np.ndarray]: A 2D array of the full arc image. 
                              The 1D spectrum is extracted before analysis.
            element_list [list[str]]: A list of strings representing the elements used in 
                                      the calibration lamp. Default is ['He', 'Ne', 'Ar']
            min_wave [int]: The minimum wavelength of the filter used in Angstroms. 
                            Default is 4700AA for V-Band.
            max_wave [int]: The maximum wavelength of the filter used in Angstroms.
                            Default is 7000AA for V-Band.
            tol [int]: The range tolerance on the hough transform.
            nbins [int]: The number of x and y bins for the hough transform.
            max_tries [int]: The max tries for the fitting. Default is 5000.
            brightness [int]: The relative brightness of the lines. The prominence passed to
                              scipy.signal.find_peaks will be the inverse of this. 
                              Default is 0.001. If your lines are very dim try 0.1.
            dist [int]: The distance between lines to be passed to scipy.signal.find_peaks.
                        Default is 100.
            debug [bool]: If true plot some debug plots.
            **kwargs: Passed to the scipy.signal.find_peaks.        

        Returns:
            The wavelength array for the spectrum. This is the same length as the x length
            of the input arc.        
        '''

        # extract a 1d crossection from the arc data
        if debug:
            print('Extracting the 1D spectrum...')

        arc1d = cls.extract_1d(arc, arc=True, background_subtract=False, debug=False)

        # Now following from https://rascal.readthedocs.io/en/latest/tutorial/keck-deimos.html
        peaks, _ = find_peaks(arc1d, prominence=1/line_brightness, distance=dist) #**kwargs)
        peaks_refined = refine_peaks(arc1d, peaks, window_width=3)

        # construct the calibrator and set hyperparams of the transform
        # and the fit
        c = Calibrator(peaks_refined, arc1d)

        c.set_calibrator_properties(num_pix=len(arc1d),
                                    plotting_library='matplotlib',
                                    log_level='info')

        c.set_hough_properties(num_slopes=10000,
                               xbins=nbins,
                               ybins=nbins,
                               min_wavelength=min_wave,
                               max_wavelength=max_wave,
                               range_tolerance=tol,
                               linearity_tolerance=50)

        c.set_ransac_properties(sample_size=5,
                                top_n_candidate=5,
                                linear=True,
                                filter_close=True,
                                ransac_tolerance=5,
                                candidate_weighted=True,
                                hough_weight=1.0)

        # add the elements to the atlas
        c.add_atlas(element_list) #Atlas(elements=element_list)

        if debug:
            print('Performing the hough transform...')
        c.do_hough_transform()

        if debug:
            c.plot_arc(save_fig=True, filename='arc.png');
            print('Performing the fit...')

        # now perform the fit
        # Order of fit_out:
        # fit_coeff, matched_peaks, matched_atlas, rms, residual, peak_util, atlas_util
        fit_out = c.fit(max_tries=500)

        # extract the wavelength array
        fit_coeff = fit_out[0]
        wave = c.polyval(c.pixel_list, fit_coeff)

        # generate some debug plots
        if debug:
            rms = fit_out[3]
            residual = fit_out[4]
            peak_util = fit_out[5]

            print("RMS: {}".format(rms))
            print("Stdev error: {} A".format(np.abs(residual).std()))
            print("Peaks utilisation rate: {}%".format(peak_util*100))

            c.plot_fit(fit_coeff,
                       plot_atlas=True,
                       log_spectrum=False,
                       tolerance=5,
                       save_fig=True,
                       filename='plot_wave_fit.png');

            c.plot_search_space(save_fig=True, filename='wave_fit_search_space.png');

        return wave
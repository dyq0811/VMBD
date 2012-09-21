#!/usr/bin/env python
'''
Created on Sep 19, 2012

@author: madmaze
'''
# Load other libraries
import pycuda.autoinit
import pycuda.gpuarray as cua
import pycuda.driver as cuda
import numpy as np
# pylab is turned off because it takes forever to load
#import pylab as pl
#from pylab import draw, figure, bla bla bla
import os
from shutil import rmtree 
from optparse import OptionParser

# Load own libraris
import olaGPU 
import imagetools
import gputools
import fitsTools
import stopwatch

# --------------------------------------------
# Global params
# Specify data path and file identifier
DATAPATH = '/DATA/LSST/FITS'
RESPATH  = '../../../DATA/results';
BASE_N = 141
FNAME = lambda i: '%s/v88827%03d-fz.R22.S11.fits' % (DATAPATH,(BASE_N+i))
ID       = 'LSST'

# Hack for chomping out a 1000x1000 chunk out of a 4000x4000px image
def loadFits(fname):
    xOffset=2000
    yOffset=0
    chunkSize=1000
    return (1. * fitsTools.readFITS(fname, use_mask=True, norm=True)[yOffset:yOffset+chunkSize,xOffset:xOffset+chunkSize])

# make/overwrite results directory
def setupResultDir(dirname, overwrite):
    if os.path.exists(dirname) and overwrite:
        try:
            rmtree(dirname)
        except:
            print "[ERROR] removing old results dir:",dirname
            exit()
            
    elif os.path.exists(dirname):
        print "[ERROR] results directory already exists, please remove or use '-o' to overwrite"
        exit()
        
    # Create results path if not existing
    try:
        os.makedirs(dirname)
    except:
        print "[ERROR] creating results dir:",dirname
        exit()
    
    print 'Results Dir: \n %s \n' % dirname

def printGpuMemStats():
    (free,total)=cuda.mem_get_info()
    print("\tGlobal mem: %2.2f%% / %.2fMB/%.2fMB free"%(free*100/total,free/(1024**2),total/(1024**2)))
    
def process(opts):
    
    #------------------------------------------------------
    # Setup dirs and vars
    #-----
    
    # dimenstion of PSF default: 20x20px
    psfSize = np.array([opts.psfSize,opts.psfSize])
    # number of PSFs default: 3x3
    psfCnt = (opts.psfCnt,opts.psfCnt)
    
    resPath = '%s/%s_sf%dx%d_csf%dx%d_maxiter%d_alpha%.2f_beta%.2f' % \
          (RESPATH,ID,psfSize[0],psfSize[1],psfCnt[0],psfCnt[1],opts.optiter,opts.f_alpha,opts.f_beta)
          
    xname = lambda i: '%s/x_%04d.png' % (resPath, i)
    yname = lambda i: '%s/y_%04d.png' % (resPath, i)
    psfname = lambda i: '%s/psf_%04d.png' % (resPath, i)
    
    setupResultDir(resPath, opts.overwrite)
    
    #------------------------------------------------------
    # Initialize PSF by averaging N frames into y_ave
    y_ave=0.0
    for n in range(opts.Ninit):
            y_ave+=loadFits(FNAME(n))
            
    y_ave = y_ave/opts.Ninit
    
    # copy to GPU
    y_gpu = cua.to_gpu(y_ave)
    
    # pad to (psfSize)+y_gpu.size+(psfSize)
    x_gpu = gputools.impad_gpu(y_gpu, psfSize-1)
    
    # create windows for OlaGPU
    # Init GPU window arrays 
    # (default windows type = "Bartlett Hann") [http://en.wikipedia.org/wiki/Window_function#Bartlett.E2.80.93Hann_window]
    x_shape = y_ave.shape + psfSize - 1
    psfSize2 = np.floor(psfSize/2)
    winaux = imagetools.win2winaux(x_shape, psfCnt, opts.psfOverlap)
    
    # print debug stats
    printGpuMemStats()
    
    #------------------------------------------------------
    # Loop over all frames and do online blind deconvolution
    for i in np.arange(1, opts.N+1):
        print "\nProcessing frame %d/%d------------------------" % (i,opts.N)
        printGpuMemStats()
        
        # Load next observed image
        y = loadFits(FNAME(i))
        
        # Compute mask for determining saturated regions
        #     - no necessary, all values < 1
        mask_gpu = 1. * cua.to_gpu(y < 1.)
        
        # Load onto GPU
        y_gpu = cua.to_gpu(y)
        
        #--------------------------------------------------
        # PSF estimation
        
        # Creat OlaGPU instance with current estimate of latent image
        X = olaGPU.OlaGPU(x_gpu, psfSize, 'valid', winaux=winaux)
        
        # PSF estimation for given estimate of latent image and current observation
        print "\trunning PSF estimation with lbfgsb minimization.."
        # fortran lbfgsb
        #psfEst = X.deconv(y_gpu, mode = 'lbfgsb', alpha = opts.f_alpha, beta = opts.f_beta,
        #     maxfun = opts.optiter, verbose = -1)
        # Note: change verbose=0 for basic out put and verbose=10 for output every 10th itr
        
        # gdirect
        psfEst = X.deconv(y_gpu, mode = 'gdirect', alpha = opts.f_alpha, beta = opts.f_beta,
             maxfun = opts.optiter, verbose = -1)
        
        # Normalize PSF kernels to sum up to one
        #    - implement on GPU to avoid copy back
        # lbfgsb
        #fs = gputools.normalize(psfEst.[0])
        # gdirect
        fs = gputools.normalize(psfEst.get())

        # ------------------------------------------------------------------------
        # Latent image estimation
        # ------------------------------------------------------------------------
        # Create OlaGPU instance with estimated PSF
        F = olaGPU.OlaGPU(fs, x_shape, 'valid', winaux=winaux)
        
        # Latent image estimation by performing one gradient descent step
        # multiplicative update is used which preserves positivity 
        factor_gpu = F.cnvtp(mask_gpu*y_gpu)/(F.cnvtp(mask_gpu*F.cnv(x_gpu))+opts.tol)
        gputools.cliplower_GPU(factor_gpu, opts.tol)
        x_gpu = x_gpu * factor_gpu
        x_max = x_gpu.get()[psfSize[0]:-psfSize[0],psfSize[1]:-psfSize[1]].max()
        
        gputools.clipupper_GPU(x_gpu, x_max)
        
        # Manually dealloc mask and y
        del mask_gpu
        del y_gpu
        del X
        del psfEst
        del fs
        #del F
        
    
    
    

if __name__ == "__main__":
    optparser = OptionParser()
    optparser.add_option("-s","--doShow", action="store_true", dest="doShow", default=False, help="show output at every timestep (Default: False)")
    optparser.add_option("-b","--backup", action="store_true", dest="backup", default=False, help="write intermediate results to disk (Default: False)")
    optparser.add_option("-l","--log", action="store_true", dest="log", default=False, help="write log to file (Default: False)")
    optparser.add_option("-o","--overwrite", action="store_true", dest="overwrite", default=False, help="overwrite existing results (Default: False)")
    optparser.add_option("-n","--nFrames", dest="N", default=100, type="int", help="Number of frames to process (Default: 100)")
    optparser.add_option("-i","--nInitFrames", dest="Ninit", default=20, type="int", help="Number of frames averaged for initialization (Default: 20)")
    optparser.add_option("--f_alpha", dest="f_alpha", default=0.0, type="float", help="promotes smoothness (Default: 0.0)")
    optparser.add_option("--f_beta", dest="f_beta", default=0.1, type="float", help="Thikhonov regularization (Default: 0.1)")
    optparser.add_option("--optiter", dest="optiter", default=50, type="float", help="number of iterations (Default: 50)")
    optparser.add_option("--tol", dest="tol", default=1e-10, type="float", help="tolerance for when to stop minimization (Default: 1e-10)")
    optparser.add_option("--psfcnt", dest="psfCnt", default=3, type="int", help="number of PSF (Default: 3)")
    optparser.add_option("--psfsize", dest="psfSize", default=20, type="int", help="size of each PSF (Default: 20)")
    optparser.add_option("--psfoverlap", dest="psfOverlap", default=0.5, type="float", help="overlap between PSFs (Default: 0.5)")
    
    (opts,args) = optparser.parse_args()
    
    print "Set Parameters:", opts, args, "\n"
    process(opts)
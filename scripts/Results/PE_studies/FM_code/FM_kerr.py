import os 
import sys

import cupy as cp
import numpy as np
import time
import matplotlib.pyplot as plt

# from lisatools.sensitivity import noisepsd_AE2,noisepsd_T # Power spectral densities
from fastlisaresponse import ResponseWrapper             # Response

from lisatools.sensitivity import get_sensitivity
from lisatools.utils.utility import AET
from lisatools.detector import scirdv1
from lisatools.detector import EqualArmlengthOrbits
from lisatools.sensitivity import AE1SensitivityMatrix

from stableemrifisher.fisher import StableEMRIFisher

# Import relevant EMRI packages
from few.waveform import GenerateEMRIWaveform
from few.trajectory.ode import PN5, SchwarzEccFlux, KerrEccEqFlux

from few.trajectory.inspiral import EMRIInspiral
from few.utils.utility import get_separatrix, get_p_at_t

from few.summation.directmodesum import DirectModeSum 
from few.summation.interpolatedmodesum import InterpolatedModeSum
from few.utils.modeselector import ModeSelector, NeuralModeSelector

# Import features from eryn
from eryn.ensemble import EnsembleSampler
from eryn.moves import StretchMove
from eryn.prior import ProbDistContainer, uniform_dist
from eryn.backends import HDFBackend

MAKE_PLOT = True
# Import parameters
sys.path.append("/home/ad/burkeol/work/KerrEccentricEquatorialFigures/scripts/PE_studies")
from EMRI_settings import (M, mu, a, p0, e0, x_I0, dist, qS, phiS, qK, phiK, Phi_phi0, Phi_theta0, Phi_r0, T, delta_t)
use_gpu = True

name_file = "M_1e7_mu_1e1_a_0p998_p0_2p12_e0_0p425_dist_5p465_SNR_30_dt_10_T_2"
# name_file = "M_1e7_mu_1e5_a_0p95_p0_23p6015_e0_0p85_dist_7p25_SNR_30_dt_10_T_2"
# name_file = "M_1e6_mu_1e1_a_0p99_p0_7p85_e0_0p7_dist_3p429_SNR_50_dt_10_T_2"
YRSID_SI = 31558149.763545603

np.random.seed(1234)

tdi_gen = "2nd generation"

order = 25  # interpolation order (should not change the result too much)
tdi_kwargs_esa = dict(
    orbits=EqualArmlengthOrbits(use_gpu=use_gpu),
    order=order,
    tdi=tdi_gen,
    tdi_chan="AE",
)  # could do "AET"

index_lambda = 8
index_beta = 7

# with longer signals we care less about this
t0 = 20000.0  # throw away on both ends when our orbital information is weird

TDI_channels = ['TDIA','TDIE']
N_channels = len(TDI_channels)

def noise_PSD_AE(f, TDI = 'TDI2'):
    """
    Inputs: Frequency f [Hz]
    Outputs: Power spectral density of noise process for TDI1 or TDI2.

    TODO: Incorporate the background!! 
    """
    # Define constants
    L = 2.5e9
    # c = 299_792_458
    c = 299758492
    x = 2*np.pi*(L/c)*f
    
    # Test mass acceleration
    Spm = (3e-15)**2 * (1 + ((4e-4)/f)**2)*(1 + (f/(8e-3))**4) * (1/(2*np.pi*f))**4 * (2 * np.pi * f/ c)**2
    # Optical metrology subsystem noise 
    Sop = (15e-12)**2 * (1 + ((2e-3)/f)**4 )*((2*np.pi*f)/c)**2
    
    S_val = (2 * Spm *(3 + 2*np.cos(x) + np.cos(2*x)) + Sop*(2 + np.cos(x))) 
    
    if TDI == 'TDI1':
        S = 8*(np.sin(x)**2) * S_val
    elif TDI == 'TDI2':
        S = 32*np.sin(x)**2 * np.sin(2*x)**2 * S_val
    return cp.asarray(S)

def zero_pad(data):
    """
    Inputs: data stream of length N
    Returns: zero_padded data stream of new length 2^{J} for J \in \mathbb{N}
    """
    N = len(data)
    pow_2 = xp.ceil(np.log2(N))
    return xp.pad(data,(0,int((2**pow_2)-N)),'constant')

def inner_prod(sig1_f,sig2_f,N_t,delta_t,PSD):
    """
    Compute stationary noise-weighted inner product
    Inputs: sig1_f and sig2_f are signals in frequency domain 
            N_t length of padded signal in time domain
            delta_t sampling interval
            PSD Power spectral density

    Returns: Noise weighted inner product 
    """
    prefac = 4*delta_t / N_t
    sig2_f_conj = xp.conjugate(sig2_f)
    return prefac * xp.real(xp.sum((sig1_f * sig2_f_conj)/PSD))

## =================== SET UP PARAMETERS =====================

N_channels = 2
xp = cp

## ===================== CHECK TRAJECTORY ====================
# 
traj = EMRIInspiral(func=KerrEccEqFlux)  # Set up trajectory module, pn5 AAK

# Compute trajectory 
if a < 0:
    a *= -1.0 
    Y0 *= -1.0

t_traj, p_traj, e_traj, Y_traj, Phi_phi_traj, Phi_r_traj, Phi_theta_traj = traj(M, mu, a, p0, e0, x_I0,
                                             Phi_phi0=Phi_phi0, Phi_theta0=Phi_theta0, Phi_r0=Phi_r0, T=T)

traj_args = [M, mu, a, e_traj[0], Y_traj[0]]
index_of_p = 3
# Check to see what value of semi-latus rectum is required to build inspiral lasting T years. 
p_new = get_p_at_t(
    traj,
    T,
    traj_args,
    index_of_p=3,
    index_of_a=2,
    index_of_e=4,
    index_of_x=5,
    xtol=2e-12,
    rtol=8.881784197001252e-16,
    bounds=None
    # bounds=[10, 150]
)


print("We require initial semi-latus rectum of ",p_new, "for inspiral lasting", T, "years")
print("Your chosen semi-latus rectum is", p0)
if p0 < p_new:
    print("Careful, the smaller body is plunging. Expect instabilities.")
else:
    print("Body is not plunging.") 
print("Final point in semilatus rectum achieved is", p_traj[-1])
print("Separatrix : ", get_separatrix(a, e_traj[-1], Y_traj[-1]))

print("Now going to load in class")

inspiral_kwargs = {"err":1e-12}

Kerr_waveform = GenerateEMRIWaveform(
        "FastKerrEccentricEquatorialFlux",
        sum_kwargs=dict(pad_output=True),
        use_gpu=use_gpu,
        inspiral_kwargs = inspiral_kwargs,
        return_list=False,
    )


# Build the response wrapper
print("Building the responses!")

EMRI_TDI_Model = ResponseWrapper(
        Kerr_waveform,
        T,
        delta_t,
        index_lambda,
        index_beta,
        t0=t0,
        flip_hx=True,  # set to True if waveform is h+ - ihx (FEW is)
        use_gpu=use_gpu,
        is_ecliptic_latitude=False,  # False if using polar angle (theta)
        remove_garbage="zero",  # removes the beginning of the signal that has bad information
        **tdi_kwargs_esa,
    )

####=======================True Responsed waveform==========================

params = [M, mu, a, p0, e0, 1.0, dist, qS, phiS, qK, phiK, Phi_phi0, Phi_theta0, Phi_r0]

print("Running the truth waveform")

Kerr_TDI_waveform = EMRI_TDI_Model(*params)
# def new_func(*params, **kwargs):
#     output = EMRI_TDI_Model(*params, **kwargs)
#     check_nan = cp.sum(cp.isnan(output)kkk
# Taper and then zero_pad signal
Kerr_FEW_TDI_pad = [zero_pad(Kerr_TDI_waveform[i]) for i in range(N_channels)]

N_t = len(Kerr_FEW_TDI_pad[0])

# Compute signal in frequency domain
Kerr_TDI_fft = xp.asarray([xp.fft.rfft(waveform) for waveform in Kerr_FEW_TDI_pad])

freq = xp.fft.rfftfreq(N_t,delta_t)
freq[0] = freq[1]   # To "retain" the zeroth frequency

# Define PSDs
freq_np = xp.asnumpy(freq)

PSD_AET = 2*[noise_PSD_AE(freq_np)]

# Clip the PSD
for i in range(2):
    PSD_AET[i][PSD_AET[i] < PSD_AET[0][0]] = PSD_AET[i][0]

# Compute optimal matched filtering SNR
SNR2_Kerr_FEW = xp.asarray([inner_prod(Kerr_TDI_fft[i],Kerr_TDI_fft[i],N_t,delta_t,PSD_AET[i]) for i in range(N_channels)])

SNR_Kerr_FEW = xp.asnumpy(xp.sum(SNR2_Kerr_FEW)**(1/2))

print("SNR for Kerr_FEW is",SNR_Kerr_FEW)

# ================== PLOT THE A CHANNEL ===================

if MAKE_PLOT == True:
    plot_direc = "/home/ad/burkeol/work/KerrEccentricEquatorialFigures/scripts/PE_studies/waveform_plots/freq_domain/"
    plt.loglog(freq_np[1:], freq_np[1:]*abs(cp.asnumpy(Kerr_TDI_fft[0][1:])), label = "Waveform frequency domain")
    plt.loglog(freq_np[1:], np.sqrt(freq_np[1:] * cp.asnumpy(PSD_AET[0][1:])), label = "TDI2 PSD")
    plt.xlabel(r'Frequency [Hz]', fontsize = 30)
    plt.ylabel(r'Magnitude',fontsize = 30)
    plt.title(fr'$(M, \mu, a, p_0, e_0, D_L)$ = {M,mu,p0,a, e0,dist}')
    plt.grid()
    plt.xlim([1e-4,freq_np[-1]])
    plt.savefig(plot_direc + name_file + "_charac_strain.png", bbox_inches = "tight")

# ============================= COMPUTE THE FISHER MATRIX =========================================
param_names = ['M','mu','a','p0','e0','dist','qS','phiS','qK','phiK','Phi_phi0','Phi_r0']
output_dir = "/home/ad/burkeol/work/KerrEccentricEquatorialFigures/scripts/PE_studies/FM_code/FM_output"


# delta = {'M': 1e-2, 'mu': 6.158482110660267e-08, 'a': 2.0484467003036454e-08, 
#         'p0': 1.4388898580034613e-08, 'e0': 5.4483966567803533e-08, 'dist': 0.0, 
#         'qS': 0.00021406661993596975, 'phiS': 6.546713737402235e-06, 'qK': 0.00011507599106301309, 
#         'phiK': 8.562664797438793e-05, 'Phi_phi0': 1e-7, 'Phi_r0': 1e-7}
# delta =  {'M': 0.0018329807108324375, 'mu': 3.3598182862837814e-08, 'a': 6.096897289553686e-09, 'p0': 8.86137744972479e-08, 'e0': 7.90186524179282e-09, 'dist': 0.0, 'qS': 0.00021406661993596975, 'phiS': 6.546713737402235e-06, 'qK': 3.866344190857403e-05, 'phiK': 8.562664797438793e-05, 'Phi_phi0': 4.138276162229581e-07, 'Phi_r0': 1.8475446331980809e-06}
# delta = {'M': 0.03359818286283781, 'mu': 6.951927961775605e-07, 'a': 6.146165146438964e-09, 'p0': 1.4738087278964292e-07, 'e0': 6.113412025222568e-07, 'dist': 0.0, 'qS': 0.00021406661993596975, 'phiS': 2.4829656973377497e-07, 'qK': 1.29902139135098e-05, 'phiK': 9.665860477143506e-06, 'Phi_phi0': 0.00254854997140627, 'Phi_r0': 0.0012843997196158193}
fish = StableEMRIFisher(M, mu, a, p0, e0, 1.0, dist, qS, phiS, qK, phiK,
                        Phi_phi0, Phi_theta0, Phi_r0, 
                        dt=delta_t, T=T, EMRI_waveform_gen=EMRI_TDI_Model, 
                        noise_kwargs=dict(TDI="TDI2"), 
                        param_names=param_names, stats_for_nerds=True, use_gpu=use_gpu, 
                        der_order=4., Ndelta=20, filename=output_dir,
                        deltas = None, #delta,
                        # log_e = log_e, # useful for sources close to zero eccentricity
                        CovEllipse=False, # will return the covariance and plot it
                        stability_plot=True, # activate if unsure about the stability of the deltas
                        window=None # addition of the window to avoid leakage
                        )

# Compute SNR and FMs
SNR = fish.SNRcalc_SEF()
fim = fish()
cov = np.linalg.inv(fim)
FM_direc = "/home/ad/burkeol/work/KerrEccentricEquatorialFigures/scripts/PE_studies/Data/Cov_Matrices/FM_cov_matrices/"
np.save(FM_direc + name_file + ".npy",cov)
breakpoint()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Jul  2 14:03:13 2022

@author: noise
"""

import torch
import numpy as np
from UNetArch import *
import matplotlib.pyplot as plt
from torchPhysicsUtils import Propagator1D, Propagator1DPadded

import scipy as sp
from scipy import stats
from scipy.optimize import curve_fit

#%% load neural network
nnre = UNet(1)
nnim = UNet(1)
nnre.load_state_dict(torch.load('/home/noise/code/pinn/inv-design-2d/pinn-metagrating-optimization/optimizeNeuralNetwork/trainedNets/nnre'))
nnim.load_state_dict(torch.load('/home/noise/code/pinn/inv-design-2d/pinn-metagrating-optimization/optimizeNeuralNetwork/trainedNets/nnim'))
nnre.cuda()
nnim.cuda()
#%%
# resolution and grid parameters
# for fields
Np = 16
p = 0.443
dx = p/Np
h = 0.6 # pillar height
x = torch.linspace(-p*6,p*6,Np*12) # grid for fields
y = torch.linspace(-p*3,p*3,Np*6)
xx,yy = torch.meshgrid(x,y)
xx = xx.T
yy = yy.T
radlocs = np.arange(-5,6)*p # radius locations
index = 2
Nx = 96
Ny = 192
wave = 0.633 # wavelength
#%%
def ssig(x, a, b):
    '''
    modified sigmoid activation function
    to produce differentiable pillars
    '''
    return 1./(1.+torch.exp(-a*(x-b)))

def mesh(radii):
    '''
    Parameters
    ----------
    radii : tensor
        11 pillar radii

    Returns
    -------
    tensor
        meshed epsilon field

    '''
    # gaussian for height of pillars
    gh = torch.exp(-yy**2/(2*(h/2)**2))
    
    # generate gaussians for x locations/radii of pillars
    g = []
    for i in range(0, len(radii)):
        g.append(torch.exp(-(xx-radlocs[i])**2/(2*radii[i]**2)))
        
    a = 100 # aggressiveness of modified sigmoid, too large of a value will cause
            # errors in gradient computation
    
    epsL = []
    for i in range(0, len(radii)):
        epsL.append(ssig(g[i],a,np.log(2))*ssig(gh,a,np.log(2)))
    eps_t = torch.stack(epsL)
    eps = torch.sum(eps_t, axis=0)*(index**2-1)+1
    
    return eps

#eps = mesh(torch.rand(11)*p/2)

def rVectorToBatchRadii(rVect, batchSize, numRadiiToStitch):
    '''
    Parameters
    ----------
    rVect : tensor
        all radii to optimize
    
    batchSize : int
        number of radii to simulate at once
    
    numRadiiToStitch : int
        number of radii to stitch at once for fullfield computation

    Returns
    -------
    batchR : tensor
        batches to simulate at a time so...
        [[0, 0, r1, r2, r3, ....],
         [rn, rn+1, rn+2, ...]

    '''
    numRows = int(np.ceil(len(rVect)/numRadiiToStitch))
    indexArr = torch.zeros((numRows, batchSize),dtype=int)
    numLeadZeros = int(5.5-numRadiiToStitch/2)
    for i in range(0,numRows):
        indexArr[i,:] = torch.arange(numRadiiToStitch*i,numRadiiToStitch*i+batchSize)
    padr = torch.nn.functional._pad(input=rVect, pad=(numLeadZeros, batchSize), mode='constant', value=0)
    batchR = padr[indexArr]
    return batchR

def rchunkToField(radii):
    '''
    Parameters
    ----------
    radii : tensor
        batch radius to simulate

    Returns
    -------
    tensor
        field from neural network

    '''
    eps = mesh(radii).reshape(1,1,Nx,Ny) # mesh
    E = nnre(eps.cuda()) # real 
    F = nnim(eps.cuda()) # imaginary
    field = E+1j*F #full field
    return field.reshape(Nx, Ny)

def batchToPatches(bR):
    '''
    Parameters
    ----------
    bR : full list of batch tensors from rVectorToBatchRadii 

    Returns
    -------
    ret : tensor
        full output field

    '''
    patches = []
    for i in range(0,len(bR)):
        patches.append(rchunkToField(bR[i]))
        
    ret = torch.stack(patches)
    return ret

def batchToEpsPatch(bR):
    '''
    for testing only
    '''
    patches = []
    for i in range(0,len(bR)):
        patches.append(mesh(bR[i]).reshape(Nx,Ny))
        
    ret = torch.stack(patches)
    return ret

def stitchPatches(patches, numPeriods):
    '''
    Parameters
    ----------
    patches : tensor
        patched fields from batchToPatches
    numPeriods : number of periods to stitch

    Returns
    -------
    result : fullfield

    '''
    numPix = int(numPeriods*Np/2)
    fullfield = []
    for i in range(0,len(patches)):
        fullfield.append(patches[i,:, 96-numPix:96+numPix])
        
    result = torch.cat(fullfield, dim=1)
    return result

def generatePerfectLens(field, f):
    '''
    Parameters
    ----------
    field : field profile
    f : focal length
    Returns
    -------
    intensity profile for perfect lens

    '''
    omega = 2*np.pi/wave
    measuredField = field[80,:] # transmission profile
    
    x = np.arange(0,len(measuredField))*dx # coordinates
    phaseProfile = torch.tensor(np.mod(2*np.pi/wave*(f-np.sqrt(x**2+f**2)),2*np.pi)) # perfect phase
    perfectLensField = torch.exp(1j*phaseProfile) #  perfect field
    
    p = Propagator1DPadded(len(measuredField), omega, f, dx, pad_factor=3., device=torch.device('cuda')) # propagator
    fieldAtFocalPlane = p.prop(perfectLensField.cuda()) # field at focal plane
    
    I = torch.abs(fieldAtFocalPlane)**2
    return I

def profileLoss(field, f, weights):
    '''
    Parameters
    ----------
    field : tensor
        neural net predicted field
    f : float
        focal length
    weights : tensor
        perfect lens profile

    Returns
    -------
    loss

    '''
    
    # normalize to 1
    weights = weights/torch.max(weights)
    
    # transmission profile
    measuredField = field[80,:]
    
    # coordinates
    x = np.arange(0,len(measuredField))*dx
    omega = 2*np.pi/wave
    
    # propagate to focal spot
    p = Propagator1DPadded(len(measuredField), omega, f, dx, pad_factor=3., device = torch.device('cuda'))
    fieldAtFocalPlane = p.prop(measuredField)
 
    loss = -torch.sum(weights*torch.abs(fieldAtFocalPlane)**2)
       
    return loss

def optGaussianProfileADAM(initR, F, nrads):
    '''
    Parameters
    ----------
    initR : initial radius
    F : float
        focal length
    nrads : int
        num radii to stitch

    Returns
    -------
    optimal radii

    '''
    
    # convert radii to optimize
    r = torch.tensor(initR.copy(), requires_grad = True, device="cpu")
    
    # loss array        
    train_loss_ar = []
    
    # number of epochs
    epochs = 100
    optimizer = torch.optim.Adam([r], lr=0.01)
    
    bR = rVectorToBatchRadii(r, 11, nrads)
    patches = batchToPatches(bR)
    result = stitchPatches(patches, nrads)
    measuredField = result[80,:]
    omega = 2*np.pi/0.633
    
    weights = generatePerfectLens(result, F)
    for epoch in range(epochs):
        with torch.no_grad():
            r.clamp_(dx,0.443/2)    
        
        
        bR = rVectorToBatchRadii(r, 11, nrads)
        patches = batchToPatches(bR)
        result = stitchPatches(patches, nrads)
        
        optimizer.zero_grad()
        l = profileLoss(result, F, weights)
        l.backward()
        
        alpha = 0.01
        
        optimizer.step()
        
        train_loss_ar.append(l.item())
        
        print(epoch, "out of", epochs)
    plt.figure()
    plt.plot(train_loss_ar)
    plt.show()
    
    return r.detach().cpu().numpy(), result

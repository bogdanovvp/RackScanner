import os
import logging
from time import time

import numpy as np
import cv2
from pydmtx import DataMatrix
from scipy.ndimage import center_of_mass, label
import pandas as pd
import re
from math import atan, sqrt
import matplotlib.pyplot as plt
from scipy import stats

log = logging.getLogger('jipylib.xpil.imgmatrix')
statistics = pd.DataFrame()
well_stats = pd.DataFrame()
failed = pd.DataFrame()
dm_read = DataMatrix(max_count = 1, 
                     timeout = 300, 
                     min_edge = 10, 
                     max_edge = 32, 
                     threshold = 5, 
                     deviation = 10,
                     shape = DataMatrix.DmtxSymbol12x12)
origin = {96:[35,40], 24:[150,150]}
well_size = 150
well_shape = (150, 150)
well_center = (75, 75)
peephole = cv2.circle(np.zeros(well_shape), well_center, 30, 1, -1)
min_size = 65
dm_size = None

class ImgMatrix(object):
    def __init__(self, filemask=None, **kwargs):
        self.filemask = filemask # filename of the plate image
        self.img = cv2.imread(filemask, 0).T # we will trasverse back the well images
        self.dg = cv2.cvtColor(self.img, cv2.COLOR_GRAY2RGB)
           
    def locate_wells_by_matching(self, debug = False):
        global dm_size
        labeled, n, crop = self.matchTemplate("template_96.png", debug = debug)
        b= 100
        if n == 96:
            self.n, n_rows, n_cols, dm_size = 96, 8, 12, 12
        else:
            labeled, m, crop = self.matchTemplate("template_24.png", debug = debug)
            if m == 24:
                self.n, n_rows, n_cols, dm_size = 24, 4, 6, 14
            else:
                # raise ValueError("%s wells detected. Should be 24 or 96." % self.n)
                print "%s and %s wells detected. Should be 24 or 96." % (n, m)
                return None
        arr = np.round(center_of_mass(crop, labeled, range(1, self.n+1))).astype(int) + b
        df = pd.DataFrame(arr, columns = ("y", "x"))
        LETTERS = np.array(list('ABCDEFGH'))
        df["row"] = LETTERS[np.arange(n_rows).repeat(n_cols)]
        df = df.sort_values(["row", 'x'])
        df["col"] = np.tile(np.arange(1, n_cols + 1), n_rows)
        df = df.set_index(['row', 'col'], drop=True)
        return df
    
    def matchTemplate(self, templ_file, debug = False):
        template = cv2.imread(templ_file)[:,:,0]
        res = cv2.matchTemplate(self.img, template, cv2.TM_CCOEFF_NORMED)
        b = 100
        crop = res[b:-b,b:-b]
        th, crop = cv2.threshold(crop, 0.6, 1, cv2.THRESH_TOZERO)
        if debug:
            plt.subplot(131); plt.imshow(self.img)
            plt.subplot(132); plt.imshow(res)
            plt.subplot(133); plt.imshow(crop)
            plt.show()
        labeled, n = label(crop)
        return labeled, n, crop
        
    def get_well_matched(self, coo):
        o = origin[self.n]
        return self.img[coo.y+o[0]:coo.y+o[0]+150, coo.x+o[1]: coo.x+o[1]+150].T.copy(order = 'C')
    #     ox, oy, dx, dy = (200, 160, 212, 206)
    #     py = oy + row * dy
    #     px = ox + col * dx
    #     well = self.img[py:py+200, px:px+200]
    #     well[0:5, :] = 0; well[:, 0:5] = 0 # for diagnostics
    #     return well[5:, 5:].copy(order = 'F')

    def mark_well(self, coo, mark):
        color = {
                'raw' : (0,255,0),
                'lsd' : (150,150,0),
                'harris' : (0,200,0),
                'unchanged' : (50,100,0),
                'rotated' : (100,100,0),
                'failed' : (255,0,0),
                'empty' : (0,0,0)
                }[mark]
        o = origin[self.n]
        cv2.circle(self.dg, (coo.x+o[0]+75, coo.y+o[1]+75), 75, color = color, thickness = 10)

    def read_rack(self, debug = False):
        start = time()
        self.wells = self.locate_wells_by_matching()
        if self.wells is None:
            return None
        self.wells = self.wells.apply(self.read_well, axis = 1)
        fail = self.wells.loc[self.wells.method == 'failed']
        if not fail.empty:
            fail = fail.apply(lambda x: x.set_value('well', self.get_well_matched(x)), axis = 1)
            fail['file'] = self.filemask
            global failed
            failed = pd.concat([failed, fail], axis = 1)
        s = self.wells.groupby('method').size()
        duration = time() - start
        log.info((s, ' %.2f s' % duration))
        self.dg_small = cv2.resize(self.dg, None,fx=0.2, fy=0.2)

        d = dict(s)
        d['time'] = duration
        d['file'] = self.filemask
        global statistics
        statistics = statistics.append(d, ignore_index=True)

        if debug: 
            plt.imshow(self.dg)
            plt.show()
            
    def read_well(self, coo):
        well = self.get_well_matched(coo)
        code, method = read_barcode(well)
        self.mark_well(coo, method)
        return pd.Series([coo.x, coo.y, code, method], index=['x', 'y', 'code', 'method'])

def read_barcode(well):
    x, thr = cv2.threshold(well, 128, 1, cv2.THRESH_BINARY)
    thr = thr * peephole
    global well_stats
    well_stats = well_stats.append({'thr_sum': thr.sum()}, ignore_index=True)
    if thr.sum() < 500:
        return ('empty', 'empty')

    code = decode_raw(well)
    if code:
        return (code, 'raw')

    code = decode_lsd(well)
    if code:
        return (code, 'lsd')
    
    code = decode_harris(well, thr_level = 128)
    if code:
        return (code, 'harris')

    code = decode(well)
    if code:
        return (code, 'unchanged')

    rotated = improve_fft(well)
    code = decode(rotated)
    if code:
        return (code, 'rotated')

    return ('failed', 'failed')

def decode_lsd(well, debug = False):
    lsd = cv2.createLineSegmentDetector()
    lines, width, prec, nfa = lsd.detect(well)

    lenghts = np.sqrt((lines[:,0,0] - lines[:,0,2])**2 + (lines[:,0,1] - lines[:,0,3])**2)
    len_idx = lenghts.argsort()
    line1 = lines[len_idx[-1]][0]
    line2 = lines[len_idx[-2]][0]
    L1 = line_params(line1)
    L2 = line_params(line2)

    its = intersection(L1, L2)

    A = dist_point(line1, its)
    B = its
    C = dist_point(line2, its)
    D = A - B + C

    box = np.array([A, B, C, D])
    box = box[[box.argmax(0)[1], box.argmin(0)[0], box.argmin(0)[1], box.argmax(0)[0]]]
    code, binarized = warp(well, box, True)
    if debug:
        contours = cv2.cvtColor(well, cv2.COLOR_GRAY2RGB)
        polyline = [box.astype(np.int32).reshape(-1, 1, 2)]
        cv2.polylines(contours, polyline, True, (0, 255, 255))
        lsd.drawSegments(contours, lines[len_idx[-2:]])
        cv2.circle(contours, its, 5, (255, 0, 0))
        plt.subplot(132); plt.imshow(contours)

        plt.subplot(133); plt.imshow(binarized)
        plt.show()
    return code



def decode_raw(well, debug = False):
    cntr = find_contour(threshold(well))
    box, u, v, a, b = fit_box(cntr)
    a, b = sorted([a, b])
    if a > 65 and a < 80 and b > 65 and b < 80:
        box = trim_contour(cntr.copy())
        code, binarized = warp(well, box, debug = True)
    elif abs(a-50) < 5 and abs(b - 94) < 5:
        center_b = box[0] + (box[2] - box[0]) / 2
        c, r = cv2.minEnclosingCircle(cntr)
        center_c = np.array(c)
        extra_point = np.int32([[2 * center_c - center_b]])
        cntr = np.append(cntr, extra_point, axis = 0)
        box, u, v, a, b = fit_box(cntr)
        code, binarized = warp(well, box, debug = True)
    # elif a > 80 or b > 80:

    else:
        code, binarized = None, np.ones_like(well)
        

    if debug:
        plt.subplot(131); plt.title('well'); plt.axis('off'); plt.imshow(well)
        raw = cv2.cvtColor(well, cv2.COLOR_GRAY2RGB)
        cv2.drawContours(raw, [np.int0(box)],0,(255,0,0),1)
        cv2.drawContours(raw, cntr, -1, (0,0,255), 1)
        plt.subplot(132); plt.title('contours'); plt.axis('off'); plt.imshow(raw)
        if 'binarized' in vars():
            plt.subplot(133); plt.title('binarized'); plt.axis('off'); plt.imshow(binarized)
        plt.show()
    return code



def decode_harris(well, debug = False, harris = None, **kwargs):
    harris = cv2.cornerHarris(well, 4, 1, 0.0)
    skew = stats.skew(harris, axis = None)
    if skew > 3.49:
        harris = cv2.morphologyEx(harris, cv2.MORPH_CLOSE, make_round_kernel(9))
        element = 'square'
    else:
        element = 'round'

    thr = threshold(harris, 0.1)
    cntr = find_contour(thr)

    box, u, v, a, b = fit_box(cntr)
    if a > min_size and b > min_size:
        box = trim_contour(cntr.copy())
        code, binarized = warp(well, box, debug = True, **kwargs)
    else:
        code, binarized = None, well
    if debug: 
        contours = cv2.cvtColor(well, cv2.COLOR_GRAY2RGB)
        contours = cv2.drawContours(contours,[np.int0(box)],0,(255,0,0),1)
        contours = cv2.drawContours(contours, cntr, -1, (0,0,255), 1)

        contours = cv2.drawContours(contours,[np.int0(box)],0,(255,0,0),1)
        contours = cv2.drawContours(contours, cntr, -1, (0,200,0), 1)
        orig_box, u, v, a, b = fit_box(cntr)
        contours = cv2.drawContours(contours,[np.int0(orig_box)],0,(255,255,0),1)
        plt.subplot(132); plt.title('contours'); plt.axis('off'); plt.imshow(contours)

        # plt.subplot(154); plt.title('warped'); plt.axis('off'); plt.imshow(warped)
        if 'binarized' in vars():
            plt.subplot(133); plt.title('binarized'); plt.axis('off'); plt.imshow(binarized)
        plt.show()
    return code



def warp(well, box, debug = False, **kwargs):
    thr_level = kwargs.pop('thr_level', 80)
    a = 120
    if box[1,1] < box[3,1]:
        src = box[0:3]
    else:
        src = box[1:4]
    M = cv2.getAffineTransform(src, np.array([[0,a],[0,0],[a,0]], dtype = "float32"))
    warped = cv2.warpAffine(well, M, (a,a)) #, flags=cv2.INTER_NEAREST
    resized = cv2.resize(warped, (dm_size, dm_size))
    # x, thr2 = cv2.threshold(resized, 80, 255, cv2.THRESH_BINARY)
    thr2 = threshold(resized, thr_level)

    # thr2[thr2 > 0] = 1
    if border_check_fix(thr2):
        # thr2[thr2 > 0] = 255
        barcode = cv2.copyMakeBorder(thr2, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value = 0)
        barcode = cv2.resize(barcode, (80,80), interpolation = cv2.INTER_NEAREST)
        barcode_cl = cv2.cvtColor(barcode, cv2.COLOR_GRAY2BGR)
        reader = DataMatrix()
        code = decode(barcode_cl, reader)
    else:
        code = None
    if debug:
        binarized = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
        mask = cv2.resize(thr2, warped.shape, interpolation = cv2.INTER_NEAREST) / 255
        if code:
            binarized[:,:,1] = warped * mask * 0.7
            binarized[:,:,0] = warped * (1 - mask)
        else:
            binarized[:,:,0] = warped * mask * 0.7
            binarized[:,:,1] = warped * (1 - mask)

        binarized[:,:,2] = 0
    else:
        binarized = well
    return code, binarized

def border_check(arr):
    size = arr.shape[0]
    assert size == arr.shape[1] and size % 2 == 0, 'Data Matrix should be square and of even size.'
    arr = arr > 0
    border = np.array([arr[-1,:], arr[:,0], arr[0,:], arr[:,-1]]).sum(1)
    if not np.array_equal(np.sort(border), [size / 2, size / 2, size, size]):
        return False
    b_index = border.argsort()
    if abs(b_index[-1] - b_index[-2]) % 2 != 1:
        return False
    return True

def border_check_fix(arr):
    borders = np.array([arr[-1,:], arr[:,0], arr[0,:], arr[:,-1]])
    borders[borders > 0] = 1 # scale down so that sums work
    if dm_size == 12:
        template = np.array([[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                             [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
                             [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1]])
    elif dm_size == 14:
        template = np.array([[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                             [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
                             [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1]])
    diffs = np.array(map(lambda x: np.logical_xor(borders, x).sum(1), template))
    wrong = diffs.min(0).sum()
    if abs(borders.sum() - 3 * dm_size) > 4:
        return False
    elif wrong > 4:
        return False
    elif wrong == 0:
        return True
    else: # fix borders
        borders = template[diffs.argmin(0)]
        borders[borders > 0] = arr.max() # scale back to original value
        arr[-1,:] = borders[0]
        arr[:,0] = borders[1]
        arr[0,:] = borders[2]
        arr[:,-1] = borders[3]
        return True

def extend_contour(img):
    if img.dtype != 'uint8': img = img.astype('uint8')
    dst, cntrs, hierarchy = cv2.findContours(img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cntrs = sorted(cntrs, key = cv2.contourArea, reverse = True)
    for cntr in cntrs:
        if cntr.min() > 1 and cntr.max() < well_size - 2:
            return cntr
    return cntr #None #np.array([[148,1],[1,1],[1,148],[148,148]], dtype=np.float32)

def find_contour(img):
    if img.dtype != 'uint8': img = img.astype('uint8')
    dst, cntrs, hierarchy = cv2.findContours(img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cntrs = sorted(cntrs, key = cv2.contourArea, reverse = True)
    for cntr in cntrs:
        if cntr.min() > 1 and cntr.max() < well_size - 2:
            return cntr
    return cntr #None #np.array([[148,1],[1,1],[1,148],[148,148]], dtype=np.float32)

def improve_fft(well):
    fft = np.fft.fftshift(np.fft.fft2(well))
    mask = np.zeros(well.shape)
    center = tuple(x/2 for x in well.shape)
    cv2.circle(mask, center, 60, color = 1, thickness = -1);
    cv2.circle(mask, center, 50, color = 0, thickness = -1);
    filtered = np.copy(fft) * mask
    blur = cv2.GaussianBlur(np.abs(filtered), (5, 5), 0)
    maximum = cv2.minMaxLoc(blur)[3]
    try:
        theta = atan(float(maximum[1]-center[1])/float(maximum[0]-center[0]))
    except ZeroDivisionError: 
        theta = 0
    M = cv2.getRotationMatrix2D(center, np.rad2deg(theta), 1)
    rotated = cv2.warpAffine(well, M, (well.shape[1], well.shape[0]))
    return rotated

def decode(img, reader = None):
    if reader is None:
        reader = dm_read
    height = img.shape[0]
    width = img.shape[1]
    code = reader.decode(width, height, img)
    if code and re.match('(\w\w)?\d{10}', code):
        return code
    else:
        return None

def trim_contour(cntr, size = 70):
    while True:
        box, u, v, a, b = fit_box(cntr)
        center = box[0] + (box[2] - box[0]) / 2
        if a > size:
            dim = 0
        elif b > size:
            dim = 1
        else:
            break
        u1, v1 = u / a, v / b
        Mb = np.column_stack((u1, v1))
        cntp = cntr.copy()[:,0,:].T
        cntp = np.dot(Mb.T, cntp)
        arr = cntp[dim, :]
        m = arr.mean()
        imax, imin = arr.argmax(), arr.argmin()
        cntp[dim, imax] = m
        cntp[dim, imin] = m
        cntr[imax, 0] = center            
        cntr[imin, 0] = center
    return box


def threshold(img, thres = None):
    if thres is None:
        tt = cv2.THRESH_OTSU
        level = 0
    elif thres > 0 and thres < 1:
        tt = cv2.THRESH_BINARY
        level = thres * img.max()
    elif type(thres) == int:
        tt = cv2.THRESH_BINARY
        level = thres
    x, thr = cv2.threshold(img, level, 255, tt)
    # print 'level: %s' % x
    return thr

def fit_box(cntr):
    box = cv2.boxPoints(cv2.minAreaRect(cntr))    
    u = box[0] - box [1]
    v = box[2] - box [1]
    return box, u, v, cv2.norm(u), cv2.norm(v)

def make_round_kernel(size):
    kernel = np.zeros((size, size), np.uint8)
    r = size / 2
    kernel = cv2.circle(kernel, (r, r), r, color = 1, thickness = -1)
    return kernel

def dist(p1, p2):
    return sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2 )

def dist_point(line, B):
    A = line[:2] if dist(line[:2], B) > dist(line[2:], B) else line[2:]
    v = B - A
    v = v / cv2.norm(v) * 68
    return B - v

def line_params(pp):
    if pp.ndim > 1:
        pp = pp[0]
    A = (pp[1] - pp[3])
    B = (pp[2] - pp[0])
    C = (pp[0]*pp[3] - pp[2]*pp[1])
    return A, B, -C

def intersection(L1, L2):
    D  = L1[0] * L2[1] - L1[1] * L2[0]
    Dx = L1[2] * L2[1] - L1[1] * L2[2]
    Dy = L1[0] * L2[2] - L1[2] * L2[0]
    if D != 0:
        x = Dx / D
        y = Dy / D
        return x,y
    else:
        return None

def main():
    pass

"""Face detection pipeline."""

import multiprocessing
import datetime
import time
import sys
import cv2
import numpy as np

import sharedmem
import mpipe
import util
import iproc
import cascade

DEVICE   = int(sys.argv[1])
WIDTH    = int(sys.argv[2])
HEIGHT   = int(sys.argv[3])
DURATION = float(sys.argv[4])  # In seconds.

# Create a process-shared (common) table keyed on timestamps
# and holding references to allocated memory and other useful values.
manager = multiprocessing.Manager()
common = manager.dict()

class Preprocessor(mpipe.OrderedWorker):
    """First step of image processing."""
    def doTask(self, tstamp):
        """Return preprocessed image."""
        iproc.preprocess(common[tstamp]['image_in'], common[tstamp]['image_pre'])
        return tstamp
 
class Detector(mpipe.OrderedWorker):
    """Detects faces."""
    def __init__(self, classifier, color):
        self._classifier = classifier
        self._color = color

    def doTask(self, tstamp):
        """Run face detection."""
        try:
            image = common[tstamp]['image_pre']
            size = np.shape(image)[:2]
            rects = list()
            r = self._classifier.detectMultiScale(
                image,
                scaleFactor=1.3,#2,
                minNeighbors=3, #4
                minSize=tuple([x/20 for x in size]),
                maxSize=tuple([x/2 for x in size]),
                )
            if len(r):
                for a,b,c,d  in r:
                    rects.append((a,b,c,d, self._color))
        except:
            print('Error in detector!!!!!!!!!!!!')

        return rects


class Postprocessor(mpipe.OrderedWorker):
    def __init__(self):
        self._prev_rects = list()

    def doTask(self, (tstamp, rects,)):
        if not rects:
            rects = self._prev_rects
        rects = [item for sublist in rects for item in sublist]
        for x1, y1, x2, y2, color in rects:
            cv2.rectangle(
                common[tstamp]['image_in'],
                (x1, y1), (x1+x2, y1+y2), 
                color=color,
                thickness=2,
                )
        self._prev_rects = rects
        return tstamp
        

class Viewer(mpipe.OrderedWorker):
    """Displays image in a window."""
    def doTask(self, tstamp):
        try:
            win_name = 'face detection'
            #cv2.namedWindow(win_name, cv2.cv.CV_WINDOW_NORMAL)
            image = common[tstamp]['image_in']
            cv2.imshow(win_name, image)
            cv2.waitKey(1)
        except:
            print('error running viewer %s !!!')
        return tstamp


class Staller(mpipe.OrderedWorker):
    def doTask(self, (tstamp, results,)):
        """Make sure the timestamp is at least a certain 
        age before propagating it further."""
        delta = datetime.datetime.now() - tstamp
        duration = datetime.timedelta(seconds=2) - delta
        if duration > datetime.timedelta():
            time.sleep(duration.total_seconds())
        return tstamp

# Monitor framerates for the given seconds past.
framerate = util.RateTicker((1,5,10))

def printStatus(tstamp):
    """Print the framerate to stdout."""
    print('%05.3f, %05.3f, %05.3f'%framerate.tick())
    return tstamp

detector_pipes = list()
for classi in cascade.classifiers:
    detector_pipes.append(
        mpipe.Pipeline(
            mpipe.Stage(
                Detector, 1, 
                classifier=classi, 
                color=cascade.colors[classi])))


# Create the image processing pipeline:
#
#            detector_pipe(s)                   viewer
#                  ||                             ||
#   preproc --> detector --> postproc --+--> filter_viewer --> staller
#                                          |
#                                          +--> printer
#
preproc = mpipe.Stage(Preprocessor)
printer = mpipe.OrderedStage(printStatus)
detector = mpipe.FilterStage(detector_pipes, max_tasks=1)
postproc = mpipe.Stage(Postprocessor)
pipe_viewer = mpipe.Pipeline(mpipe.Stage(Viewer))
filter_viewer = mpipe.FilterStage((pipe_viewer,), max_tasks=2)
staller = mpipe.Stage(Staller)
preproc.link(detector)
detector.link(postproc)
postproc.link(filter_viewer)
postproc.link(printer)
filter_viewer.link(staller)
pipe_iproc = mpipe.Pipeline(preproc)

# Create an auxiliary process (modeled as a one-task pipeline)
# that simply pulls results from the image processing pipeline
# and deallocates the associated memory.
def pull(task):
    for tstamp in pipe_iproc.results():
        del common[tstamp]
pipe_pull = mpipe.Pipeline(mpipe.UnorderedStage(pull))
pipe_pull.put(True)  # Start it up right away.
pipe_pull.put(None)  # It's a single-task (startup task) pipeline.

# Create the OpenCV video capture object.
cap = cv2.VideoCapture(DEVICE)
cap.set(3, WIDTH)
cap.set(4, HEIGHT)

# Run the video capture loop, allocating shared memory
# and feeding the image processing pipeline.
now = datetime.datetime.now()
end = now + datetime.timedelta(seconds=DURATION)
while end > now:

    # Mark the timestamp. This is the index by which 
    # image procesing stages will access allocated memory.
    now = datetime.datetime.now()

    # Capture the image.
    hello, image = cap.read()

    # Allocate shared memory for a copy of the input image.
    shape = np.shape(image)
    dtype = image.dtype
    image_in = sharedmem.empty(shape, dtype)
    image_pre  = sharedmem.empty(shape[:2], dtype)

    # Copy the input image to its shared memory version.
    image_in[:] = image.copy()
    
    # Add to the common table.
    common[now] = {
        'image_in'   : image_in,   # Input image.
        'image_pre'  : image_pre,  # Preprocessed image.
        }

    # Put the timestamp on the image processing pipeline.
    pipe_iproc.put(now)

# Capturing of video is done. Now let's shut down all
# pipelines by sending them the "stop" task.
pipe_iproc.put(None)
for result in pipe_pull.results():
    pass
for pipe in detector_pipes:
    pipe.put(None)
    for result in pipe.results():
        pass
pipe_viewer.put(None)
for result in pipe_viewer.results():
    pass

# The end.

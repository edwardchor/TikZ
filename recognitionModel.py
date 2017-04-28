from batch import BatchIterator
from language import *
from render import render,animateMatrices
from utilities import *
from distanceMetrics import blurredDistance,asymmetricBlurredDistance

import tarfile
import sys
import tensorflow as tf
import os
import io
from time import time
import pickle
import cProfile

# The data is generated on a  MAXIMUMCOORDINATExMAXIMUMCOORDINATE grid
# We can interpolate between stochastic search and neural networks by downsampling to a smaller grid
APPROXIMATINGGRID = MAXIMUMCOORDINATE
def coordinate2grid(c): return c*MAXIMUMCOORDINATE/APPROXIMATINGGRID
def grid2coordinate(g): return g*APPROXIMATINGGRID/MAXIMUMCOORDINATE

learning_rate = 0.001
TESTINGFRACTION = 0.1

[STOP,CIRCLE,LINE,RECTANGLE] = range(4)


def loadExamples(numberOfExamples, dummyImages = True):
    noisyTrainingData = "noisy" in sys.argv
    
    if os.path.isfile('/om/user/ellisk/syntheticTrainingData.tar'):
        handle = '/om/user/ellisk/syntheticTrainingData.tar'
    else:
        handle = 'syntheticTrainingData.tar'
    print "Loading data from",handle
    handle = tarfile.open(handle)
    
    # just load everything into RAM - faster that way. screw you tar
    members = {}
    for member in handle:
        if member.name == '.': continue
        stuff = handle.extractfile(member)
        members[member.name] = stuff.read()
        stuff.close()
    handle.close()

    print "Loaded tar file into RAM: %d entries."%len(members)
    
    programNames = [ "./randomScene-%d.p"%(j)
                     for j in range(numberOfExamples) ]
    programs = [ pickle.load(io.BytesIO(members[n])) for n in programNames ]

    print "Loaded pickles."

    startingExamples = []
    endingExamples = []
    target = {}

    # for debugging purposes / analysis, keep track of the target line
    targetLine = []

    startTime = time()
    # get one example from each line of each program
    for j,program in enumerate(programs):
        if j%10000 == 1:
            print "Loaded %d/%d programs"%(j - 1,len(programs))
        trace = [ "./randomScene-%d-%d.png"%(j, k) for k in range(len(program)) ]
        noisyTarget = "./randomScene-%d-noisy.png"%(j) if noisyTrainingData else trace[-1]
        # cache the images
        for imageFilename in [noisyTarget] + trace:
            cacheImage(imageFilename, members[imageFilename])
        if not dummyImages:
            trace = loadImages(trace)
            noisyTarget = loadImage(noisyTarget)
        
        targetImage = trace[-1]
        currentImage = "blankImage" if dummyImages else np.zeros(targetImage.shape)
        for k,l in enumerate(program.lines):
            startingExamples.append(currentImage)
            endingExamples.append(noisyTarget)
            targetLine.append(l)
            currentImage = trace[k]
            for j,t in enumerate(PrimitiveDecoder.extractTargets(l)):
                if not j in target: target[j] = []
                target[j].append(t)
        # end of program
        startingExamples.append(targetImage)
        endingExamples.append(noisyTarget)
        targetLine.append(None)
        for j in target:
            target[j] += [STOP] # should be zero and therefore valid for everyone
            
    targetVectors = [np.array(target[j]) for j in sorted(target.keys()) ]

    print "loaded images in",(time() - startTime),"s"
    print "target dimensionality:",len(targetVectors)

    return np.array(startingExamples), np.array(endingExamples), targetVectors, np.array(targetLine)

class StandardPrimitiveDecoder():
    def makeNetwork(self,imageRepresentation):
        # A placeholder for each target
        self.targetPlaceholder = [ tf.placeholder(tf.int32, [None]) for _ in self.outputDimensions ]
        if not hasattr(self, 'hiddenSizes'):
            self.hiddenSizes = [None]*len(self.outputDimensions)

        # A prediction for each target
        self.prediction = []
        # populate self.production
        predictionInputs = imageRepresentation
        for j,d in enumerate(self.outputDimensions):
            if self.hiddenSizes[j] == None or self.hiddenSizes[j] == 0:
                self.prediction.append(tf.layers.dense(predictionInputs, d, activation = None))
            else:
                intermediateRepresentation = tf.layers.dense(predictionInputs,
                                                             self.hiddenSizes[j],
                                                             activation = tf.nn.sigmoid)
                self.prediction.append(tf.layers.dense(intermediateRepresentation, d, activation = None))
            predictionInputs = tf.concat([predictionInputs,
                                          tf.one_hot(self.targetPlaceholder[j], d)],
                                         axis = 1)
        # "hard" predictions (integers)
        self.hard = [ tf.cast(tf.argmax(p,dimension = 1),tf.int32) for p in self.prediction ]

        # "soft" predictions (logits)
        self.soft = [ tf.nn.log_softmax(p) for p in self.prediction ]

    def loss(self):
        return sum([ tf.nn.sparse_softmax_cross_entropy_with_logits(labels = l, logits = p)
                     for l,p in zip(self.targetPlaceholder, self.prediction) ])
    def accuracyVector(self):
        '''For each example in the batch, do hard predictions match the target? ty = [None,bool]'''
        return reduce(tf.logical_and,
                      [tf.equal(h,t) for h,t in zip(self.hard,self.targetPlaceholder)])
    def placeholders(self): return self.targetPlaceholder

    @property
    def token(self): return self.__class__.token

    def beamTrace(self, session, feed, beamSize):
        originalFeed = feed
        feed = dict([(k,feed[k]) for k in feed])
        
        traces = [(0.0,[])]
        for j in range(len(self.outputDimensions)):
            for k in range(j):
                feed[self.targetPlaceholder[k]] = np.array([ t[1][k] for t in traces ])
            for p in originalFeed:
                feed[p] = np.repeat(originalFeed[p], len(traces), axis = 0)
            soft = session.run(self.soft[j], feed_dict = feed)
            traces = [(s + coordinateScore, trace + [coordinateIndex])
                  for traceIndex,(s,trace) in enumerate(traces)
                  for coordinateIndex,coordinateScore in enumerate(soft[traceIndex]) ]
            traces = sorted(traces, key = lambda t: -t[0])[:beamSize]
        return traces




class CircleDecoder(StandardPrimitiveDecoder):
    token = CIRCLE
    languagePrimitive = Circle
    
    def __init__(self, imageRepresentation):
        self.outputDimensions = [APPROXIMATINGGRID,APPROXIMATINGGRID] # x,y
        self.makeNetwork(imageRepresentation)
    
    def beam(self, session, feed, beamSize):
        return [(s, Circle(AbsolutePoint(Number(grid2coordinate(x)),Number(grid2coordinate(y))),Number(1)))
                for s,[x,y] in self.beamTrace(session, feed, beamSize) ]

    @staticmethod
    def extractTargets(l):
        if isinstance(l,Circle):
            return [coordinate2grid(l.center.x.n),
                    coordinate2grid(l.center.y.n)]
        return [0,0]

class RectangleDecoder(StandardPrimitiveDecoder):
    token = RECTANGLE
    languagePrimitive = Rectangle

    def __init__(self, imageRepresentation):
        self.outputDimensions = [APPROXIMATINGGRID,APPROXIMATINGGRID,APPROXIMATINGGRID,APPROXIMATINGGRID] # x,y
        self.makeNetwork(imageRepresentation)
            

    def beam(self, session, feed, beamSize):
        return [(s, Rectangle.absolute(grid2coordinate(x1),
                                       grid2coordinate(y1),
                                       grid2coordinate(x2),
                                       grid2coordinate(y2)))
                for s,[x1,y1,x2,y2] in self.beamTrace(session, feed, beamSize)
                if x1 != x2 and y1 != y2]

    @staticmethod
    def extractTargets(l):
        if isinstance(l,Rectangle):
            return [coordinate2grid(l.p1.x.n),
                    coordinate2grid(l.p1.y.n),
                    coordinate2grid(l.p2.x.n),
                    coordinate2grid(l.p2.y.n)]
        return [0]*4

class LineDecoder(StandardPrimitiveDecoder):
    token = LINE
    languagePrimitive = Line

    def __init__(self, imageRepresentation):
        self.outputDimensions = [APPROXIMATINGGRID,APPROXIMATINGGRID,APPROXIMATINGGRID,APPROXIMATINGGRID,2,2] # x,y for beginning and end; arrow/-
        self.hiddenSizes = [None,
                            32,
                            32,
                            32,
                            None,
                            None]
        self.makeNetwork(imageRepresentation)
    
    def beam(self, session, feed, beamSize):
        return [(s, Line.absolute(Number(grid2coordinate(x1)),
                                  Number(grid2coordinate(y1)),
                                  Number(grid2coordinate(x2)),
                                  Number(grid2coordinate(y2)),
                                  arrow = arrow,solid = solid))
                for s,[x1,y1,x2,y2,arrow,solid] in self.beamTrace(session, feed, beamSize)
                if (x1 - x2)*(x1 - x2) + (y1 - y2)*(y1 - y2) > 0 ]

    @staticmethod
    def extractTargets(l):
        if isinstance(l,Line):
            return [coordinate2grid(l.points[0].x.n),
                    coordinate2grid(l.points[0].y.n),
                    coordinate2grid(l.points[1].x.n),
                    coordinate2grid(l.points[1].y.n),
                    int(l.arrow),
                    int(l.solid)]
        return [0]*6

class StopDecoder():
    def __init__(self, imageRepresentation):
        self.outputDimensions = []
    def loss(self): return 0.0
    token = STOP
    languagePrimitive = None
    def placeholders(self): return []
    def softPredictions(self): return []
    @staticmethod
    def extractTargets(_): return []

class PrimitiveDecoder():
    # It might matter in which order these classes are listed.
    # Because you predict circle targets, then rectangle targets, then line targets
    decoderClasses = [CircleDecoder,
                      RectangleDecoder,
                      LineDecoder,
                      StopDecoder]
    def __init__(self, imageRepresentation):
        self.decoders = [k(imageRepresentation) for k in PrimitiveDecoder.decoderClasses]

        self.prediction = tf.layers.dense(imageRepresentation, len(self.decoders))
        self.hard = tf.cast(tf.argmax(self.prediction,dimension = 1),tf.int32)
        self.soft = tf.nn.log_softmax(self.prediction)
        self.targetPlaceholder = tf.placeholder(tf.int32, [None])
        self.imageRepresentation = imageRepresentation

    def loss(self):
        # the first label is for the primitive category
        ll = tf.reduce_sum(tf.nn.sparse_softmax_cross_entropy_with_logits(labels = self.targetPlaceholder,
                                                                          logits = self.prediction))
        for decoder in self.decoders:
            decoderLosses = decoder.loss()
            decoderMask = tf.cast(tf.equal(self.targetPlaceholder, decoder.token), tf.float32)
            decoderLoss = tf.reduce_sum(tf.multiply(decoderMask,decoderLosses))
            ll += decoderLoss

        return ll

    def accuracy(self):
        a = tf.equal(self.hard,self.targetPlaceholder)
        for decoder in self.decoders:
            if decoder.token != STOP:
                a = tf.logical_and(a,
                                   tf.logical_or(decoder.accuracyVector(),
                                                 tf.not_equal(self.hard,decoder.token)))
        return tf.reduce_mean(tf.cast(a, tf.float32))

    def placeholders(self):
        p = [self.targetPlaceholder]
        for d in self.decoders: p += d.placeholders()
        return p

    @staticmethod
    def extractTargets(l):
        '''Given a line of code l, what is the array of targets (int's) we expect the decoder to produce?'''
        t = [STOP]
        for d in PrimitiveDecoder.decoderClasses:
            if isinstance(l,d.languagePrimitive):
                t = [d.token]
                break
        for d in PrimitiveDecoder.decoderClasses:
            t += d.extractTargets(l)
        return t

    def beam(self, session, feed, beamSize):
        # to accelerate beam decoding, we can cash the image representation
        [tokenScores,imageRepresentation] = session.run([self.soft,self.imageRepresentation], feed_dict = feed)
        tokenScores = tokenScores[0]
        # print "token scores ",
        # for s in tokenScores: print s," "
        # print "\nToken rectangle score: %f"%tokenScores[RectangleDecoder.token]
        feed[self.imageRepresentation] = imageRepresentation
        
        b = [(tokenScores[STOP], None)] # STOP
        for d in self.decoders:
            if d.token == STOP: continue
            b += [ (s + tokenScores[d.token], program)
                   for (s, program) in d.beam(session, feed, beamSize) ]
        # for s,p in b:
        #     print s,p
#        assert False
        return b

class RecognitionModel():
    def __init__(self):
        # current and goal images
        self.currentPlaceholder = tf.placeholder(tf.float32, [None, 256, 256])
        self.goalPlaceholder = tf.placeholder(tf.float32, [None, 256, 256])

        imageInput = tf.stack([self.currentPlaceholder,self.goalPlaceholder], axis = 3)

        initialDilation = 1
        horizontalKernels = tf.layers.conv2d(inputs = imageInput,
                                             filters = 4,
                                             kernel_size = [16/initialDilation,4/initialDilation],
                                             padding = "same",
                                             activation = tf.nn.relu,
                                             dilation_rate = initialDilation,
                                             strides = 1)
        verticalKernels = tf.layers.conv2d(inputs = imageInput,
                                             filters = 4,
                                             kernel_size = [4/initialDilation,16/initialDilation],
                                             padding = "same",
                                             activation = tf.nn.relu,
                                             dilation_rate = initialDilation,
                                             strides = 1)
        squareKernels = tf.layers.conv2d(inputs = imageInput,
                                             filters = 6,
                                             kernel_size = [8/initialDilation,8/initialDilation],
                                             padding = "same",
                                             activation = tf.nn.relu,
                                             dilation_rate = initialDilation,
                                             strides = 1)
        c1 = tf.concat([horizontalKernels,verticalKernels,squareKernels], axis = 3)
        c1 = tf.layers.max_pooling2d(inputs = c1,
                                     pool_size = 8,
                                     strides = 4,
                                     padding = "same")
        print c1

        numberOfFilters = [10]
        kernelSizes = [8]
        
        poolSizes = [4]
        poolStrides = [4]
        nextInput = c1
        for filterCount,kernelSize,poolSize,poolStride in zip(numberOfFilters,kernelSizes,poolSizes,poolStrides):
            c1 = tf.layers.conv2d(inputs = nextInput,
                                  filters = filterCount,
                                  kernel_size = [kernelSize,kernelSize],
                                  padding = "same",
                                  activation = tf.nn.relu,
                                  strides = 1)
            c1 = tf.layers.max_pooling2d(inputs = c1,
                                         pool_size = poolSize,
                                         strides = poolStride,
                                         padding = "same")
            print "Convolution output:",c1
            nextInput = c1
        c1d = int(c1.shape[1]*c1.shape[2]*c1.shape[3])
        print "fully connected input dimensionality:",c1d

        f1 = tf.reshape(c1, [-1, c1d])

        self.decoder = PrimitiveDecoder(f1)
        self.loss = self.decoder.loss()
        self.averageAccuracy = self.decoder.accuracy()

        self.optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate).minimize(self.loss)        


    def train(self, numberOfExamples, checkpoint = "/tmp/model.checkpoint"):
        partialImages,targetImages,targetVectors,_ = loadExamples(numberOfExamples)
        
        initializer = tf.global_variables_initializer()
        iterator = BatchIterator(50,tuple([partialImages,targetImages] + targetVectors),
                                 testingFraction = TESTINGFRACTION, stringProcessor = loadImage)
        iterator.registerPlaceholders([self.currentPlaceholder, self.goalPlaceholder] +
                                      self.decoder.placeholders())
        saver = tf.train.Saver()

        flushEverything()

        with tf.Session() as s:
            s.run(initializer)
            for e in range(20):
                epicLoss = []
                epicAccuracy = []
                for feed in iterator.epochFeeds():
                    _,l,accuracy = s.run([self.optimizer, self.loss, self.averageAccuracy],
                                         feed_dict = feed)
                    epicLoss.append(l)
                    epicAccuracy.append(accuracy)
                print "Epoch %d: accuracy = %f, loss = %f"%((e+1),sum(epicAccuracy)/len(epicAccuracy),sum(epicLoss)/len(epicLoss))
                testingAccuracy = [ s.run(self.averageAccuracy, feed_dict = feed) for feed in iterator.testingFeeds() ]
                print "\tTesting accuracy = %f"%(sum(testingAccuracy)/len(testingAccuracy))
                print "Saving checkpoint: %s" % saver.save(s, checkpoint)
                flushEverything()

    def analyzeFailures(self, numberOfExamples, checkpoint):
        partialImages,targetImages,targetVectors,targetLines = loadExamples(numberOfExamples)
        iterator = BatchIterator(1,tuple([partialImages,targetImages] + targetVectors + [targetLines]),
                                 testingFraction = TESTINGFRACTION, stringProcessor = loadImage)
        iterator.registerPlaceholders([self.currentPlaceholder, self.goalPlaceholder] +
                                      self.decoder.placeholders() + [None])
        saver = tf.train.Saver()
        failureLog = [] # pair of current goal
        targetRanks = []
        k = 0

        with tf.Session() as s:
            saver.restore(s,checkpoint)
            for feed in iterator.testingFeeds():
                targetLine = feed[None]
                del feed[None]
                k += 1
                accuracy = s.run(self.averageAccuracy,
                                 feed_dict = feed)
                assert accuracy == 0.0 or accuracy == 1.0
                if accuracy < 0.5:
                    # decode the action preferred by the model
                    topHundred = self.decoder.beam(s, {self.currentPlaceholder: feed[self.currentPlaceholder],
                                                       self.goalPlaceholder: feed[self.goalPlaceholder]}, 100)
                    topHundred.sort(key = lambda foo: foo[0], reverse = True)
                    preferredLine = topHundred[0][1]
                    preferredLineHumanReadable = str(preferredLine)
                    preferredLine = "\n%end of program\n" if preferredLine == None else preferredLine.TikZ()
                    # check to see the rank of the correct line, because it wasn't the best
                    targetLine = str(targetLine[0])
                    topHundred = [str(l) for _,l in topHundred]
                    print "Target line (not model preference):",targetLine
                    print "Model preference:",preferredLineHumanReadable
                    if targetLine in topHundred:
                        print "Target line has rank %d in beam"%(1 + topHundred.index(targetLine))
                        targetRanks.append(1 + topHundred.index(targetLine))
                    else:
                        print "Target lie not in beam."
                        targetRanks.append(None)
                    
                    failureLog.append((feed[self.currentPlaceholder][0], feed[self.goalPlaceholder][0], preferredLine))
                    if len(failureLog) > 100:
                        break
                else:
                    pass
                    # decode the action preferred by the model
                    # preferredLine = max(self.decoder.beam(s, {self.currentPlaceholder: feed[self.currentPlaceholder],
                    #                                           self.goalPlaceholder: feed[self.goalPlaceholder]}, 1),
                    #                     key = lambda foo: foo[0])[1]
                    # preferredLine = "\n%end of program\n" if preferredLine == None else str(preferredLine)
                    # print preferredLine
                    # showImage(feed[self.currentPlaceholder][0])
                    # showImage(feed[self.goalPlaceholder][0])
                    
        print "Failures:",len(failureLog),'/',k
        successfulTargetRanks = [ r for r in targetRanks if r != None ]
        print "In beam %d/%d of the time."%(len(successfulTargetRanks),len(targetRanks))
        print "Average successful target rank: %f"%(sum(successfulTargetRanks)/float(len(successfulTargetRanks)))
        print "Successful target ranks: %s"%(str(successfulTargetRanks))
        
        for j,(c,g,l) in enumerate(failureLog):
            saveMatrixAsImage(c*255,"failures/%d-current.png"%j)
            saveMatrixAsImage(g*255,"failures/%d-goal.png"%j)
            pixels = render([l],yieldsPixels = True,canvas = (MAXIMUMCOORDINATE,MAXIMUMCOORDINATE))[0]
            pixels = 1.0 - pixels
            saveMatrixAsImage(pixels*255 + 255*c,"failures/%d-predicted.png"%j)
                

    def beam(self, targetImage, checkpoint = "/tmp/model.checkpoint", beamSize = 10, beamLength = 10):

        # place where we will save the parses
        parseDirectory = targetImage[:-4] + "-parses"
        
        totalNumberOfRenders = 0
        targetImage = loadImage(targetImage)
        #showImage(targetImage)
        targetImage = np.reshape(targetImage,(256,256))
        beam = [{'program': [],
                 'output': np.zeros(targetImage.shape),
                 'logLikelihood': 0.0}]
        # once a program is finished we wrap it up in a sequence object
        def finished(x): return isinstance(x['program'], Sequence)

        finishedPrograms = []
        
        saver = tf.train.Saver()
        with tf.Session() as s:
            saver.restore(s,checkpoint)

            for iteration in range(beamLength):
                children = []
                startTime = time()
                for parent in beam:
                    feed = {self.currentPlaceholder: np.array([parent['output']]),
                            self.goalPlaceholder: np.array([targetImage])}

                    
                    for childScore,suffix in self.decoder.beam(s, feed, beamSize):
                        if suffix == None:
                            k = Sequence(parent['program'])
                        else:
                            k = parent['program'] + [suffix]
                        children.append({'program': k,
                                         'logLikelihood': parent['logLikelihood'] + childScore})
                print "Ran neural network beam in %f seconds"%(time() - startTime)

                beam = children
                
                beam = [ n for n in children
                         if not (n['program'] if finished(n) else Sequence(n['program'])).hasCollisions() ]
                beam = sorted(beam, key = lambda c: -c['logLikelihood'])
                beam = beam[:beamSize]
                
                startTime = time()
                outputs = render([ (n['program'] if finished(n) else Sequence(n['program'])).TikZ()
                                   for n in beam ],
                                 yieldsPixels = True,
                                 canvas = (MAXIMUMCOORDINATE,MAXIMUMCOORDINATE))
                print "Rendered in %f seconds"%(time() - startTime)
                totalNumberOfRenders += len(beam)
                for n,o in zip(beam,outputs): n['output'] = 1.0 - o

                print "Iteration %d: %d total renders.\n"%(iteration+1,totalNumberOfRenders)

                for n in beam:
                    n['distance'] = asymmetricBlurredDistance(targetImage, n['output'])

                beam = sorted(beam, key = lambda c: c['distance'])

                if len(beam) > beamSize:
                    # only keep things in the beam if they produce unique outputs. encourages diversity
                    beam = [n for j,n in enumerate(beam)
                            if all([ not np.array_equal(n['output'], m['output']) for m in beam[:j] ])]
                beam = beam[:beamSize]
                

                for n in beam:
                    p = n['program']
                    if not finished(n): p = Sequence(p)
                    print "Program in beam: %s\n"%(str(p))
                    print "Blurred distance: %f"%n['distance']
                    print "Pixel wise distance: %f"%(np.sum(np.abs(n['output'] - targetImage)))
                    print "\n"
                
                # record all of the finished programs
                finishedPrograms += [ n for n in beam if finished(n) ]
                # Remove all of the finished programs
                beam = [ n for n in beam if not finished(n) ]
                if beam == []:
                    print "Empty beam."
                    break

            print "Finished programs, sorted by likelihood:"
            os.system('rm -r %s'%(parseDirectory))
            os.system('mkdir %s'%(parseDirectory))
            finishedPrograms.sort(key = lambda n: -n['logLikelihood'])
            for j,n in enumerate(finishedPrograms):
                print "Finished program: log likelihood %f"%(n['logLikelihood'])
                print n['program']
                saveMatrixAsImage(n['output']*255, "%s/%d.png"%(parseDirectory, j))
                print "Absolute pixel-wise distance: %f"%(np.sum(np.abs(n['output'] - targetImage)))
                print "Blurred distance: %f"%blurredDistance(targetImage, n['output'])
                print ""
                    

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'test':
        RecognitionModel().beam(sys.argv[2],
                                beamSize = 100,
                                beamLength = 13,
                                checkpoint = "checkpoints/model.checkpoint")
    elif sys.argv[1] == 'analyze':
        RecognitionModel().analyzeFailures(100000, checkpoint = "checkpoints/model.checkpoint")
    elif sys.argv[1] == 'train':
        RecognitionModel().train(100000, checkpoint = "checkpoints/model.checkpoint")
    elif sys.argv[1] == 'profile':
        cProfile.run('loadExamples(100000)')

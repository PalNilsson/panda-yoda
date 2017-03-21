import commands
import datetime
import json
import logging
import os
import shutil
import socket
import sys
import time
import pickle
import signal
import threading
import traceback
from Queue import Queue
from mpi4py import MPI
from pandayoda.droid import DroidStager
from pandayoda.yoda import Interaction,Database,signal_block
from EventServerJobManager import EventServerJobManager
from pandayoda import localcmd

logger = logging.getLogger(__name__)


class Droid(threading.Thread):
   def __init__(self, globalWorkingDir, localWorkingDir, outputDir=None, coreCount=None):
      super(Droid,self).__init__()
      self.__globalWorkingDir = globalWorkingDir
      self.__localWorkingDir = localWorkingDir
      self.__currentDir = None
      self.__comm = Interaction.Requester()
      self.__esJobManager = None
      self.__isFinished = False
      self.__rank = self.__comm.getRank()
      logger.info("Rank %s: Global working dir: %s",self.__rank, self.__globalWorkingDir)
      if not os.environ.has_key('PilotHomeDir'):
         os.environ['PilotHomeDir'] = self.__globalWorkingDir

      self.initWorkingDir()
      logger.info("Rank %s: Current working dir: %s",self.__rank, self.__currentDir)

      self.__jobId = None
      self.__startTimeOneJobDroid = None
      self.__cpuTimeOneJobDroid = None
      self.__poolFileCatalog = None
      self.__inputFiles = None
      self.__copyInputFiles = None
      self.__preSetup = None
      self.__postRun = None
      self.__ATHENA_PROC_NUMBER = 1
      self.__firstGetEventRanges = True
      self.__outputDir = outputDir

      self.__yodaToOS = False

      self.__hostname = socket.getfqdn()

      self.__outputs = Queue()
      self.__jobMetrics = {}
      self.__stagerThread = None

      self.__stop = False

      signal.signal(signal.SIGTERM, self.stop)
      signal.signal(signal.SIGQUIT, self.stop)
      signal.signal(signal.SIGSEGV, self.stop)
      signal.signal(signal.SIGXCPU, self.stop)
      signal.signal(signal.SIGUSR1, self.stop)
      signal.signal(signal.SIGBUS, self.stop)

   def initWorkingDir(self):
      # Create separate working directory for each rank
      curdir = os.path.abspath (self.__localWorkingDir)
      wkdirname = "rank_%s" % str(self.__rank)
      wkdir  = os.path.abspath (os.path.join(curdir,wkdirname))
      if not os.path.exists(wkdir):
          os.makedirs (wkdir)
      os.chdir (wkdir)
      self.__currentDir = wkdir

   def postExecJob(self):
      if self.__copyInputFiles and self.__inputFiles is not None and self.__poolFileCatalog is not None:
         for inputFile in self.__inputFiles:
            localInputFile = os.path.join(os.getcwd(), os.path.basename(inputFile))
            logger.debug("Rank %s: Remove input file: %s",self.__rank, localInputFile)
            os.remove(localInputFile)

      if self.__globalWorkingDir != self.__localWorkingDir:
         command = "cp -fr " + self.__currentDir + " " + self.__globalWorkingDir
         logger.debug("Rank %s: copy files from local working directory to global working dir(cmd: %s)",self.__rank, command)
         status, output = commands.getstatusoutput(command)
         logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)

      if self.__postRun and self.__esJobManager:
         self.__esJobManager.postRun(self.__postRun)

   def setup(self, job):
      global logger
      logger.info(' Droid.setup()')
      try:
         self.__jobId = job.get("jobId", None)
         self.__startTimeOneJobDroid = time.time()
         self.__cpuTimeOneJobDroid = os.times()
         self.__poolFileCatalog = job.get('PoolFileCatalog', None)
         self.__inputFiles = job.get('InputFiles', None)
         self.__copyInputFiles = job.get('CopyInputFiles', False)
         self.__preSetup = job.get('PreSetup', None)
         self.__postRun = job.get('PostRun', None)

         self.__yodaToOS = job.get('yodaToOS', False)

         self.__ATHENA_PROC_NUMBER = int(job.get('ATHENA_PROC_NUMBER', 1))
         if self.__ATHENA_PROC_NUMBER < 1:
            raise Exception('ATHENA_PROC_NUMBER = ' + str(self.__ATHENA_PROC_NUMBER) + ' must be at least 1 to make sense.')
         #job["AthenaMPCmd"] = "export ATHENA_PROC_NUMBER=" + str(self.__ATHENA_PROC_NUMBER) + "; " + job["AthenaMPCmd"]
         job['AthenaMPCmd'] = localcmd.getLocalCmd(job)
         self.__jobWorkingDir = job.get('GlobalWorkingDir', None)
         #if self.__jobWorkingDir:
         #   self.__jobWorkingDir = os.path.join(self.__jobWorkingDir, 'rank_%s' % self.__rank)
         #   if not os.path.exists(self.__jobWorkingDir):
         #      os.makedirs(self.__jobWorkingDir)
         #   os.chdir(self.__jobWorkingDir)
         #   logFile = os.path.join(self.__jobWorkingDir, 'Droid.log')
         #   logging.basicConfig(filename=logFile, level=logging.DEBUG)
         #   logger = Logger.Logger()

         if (self.__copyInputFiles and 
                  self.__inputFiles is not None and 
                  self.__poolFileCatalog is not None):
            for inputFile in self.__inputFiles:
               shutil.copy(inputFile, './')

            pfc_name = os.path.basename(self.__poolFileCatalog)
            pfc_name = os.path.join(os.getcwd(), pfc_name)
            pfc_name_back = pfc_name + ".back"
            shutil.copy2(self.__poolFileCatalog, pfc_name_back)
            with open(pfc_name, 'wt') as pfc_out:
               with open(pfc_name_back, 'rt') as pfc_in:
                  for line in pfc_in:
                     pfc_out.write(line.replace('HPCWORKINGDIR', os.getcwd()))
               
            #job["AthenaMPCmd"] = job["AthenaMPCmd"].replace('HPCWORKINGDIR', os.getcwd())

         self.__esJobManager = EventServerJobManager(self.__rank, self.__ATHENA_PROC_NUMBER, workingDir=self.__jobWorkingDir)
         status, output = self.__esJobManager.preSetup(self.__preSetup)
         if status != 0:
            return False, output

         status, output = self.startStagerThread(job)
         if status != 0:
            raise Exception("Rank %s: failed to start stager thread(status: %s, output: %s)",self.__rank, status, output)
            

         # self.__esJobManager.initMessageThread(socketname='EventService_EventRanges', context='local')
         # self.__esJobManager.initTokenExtractorProcess(job["TokenExtractCmd"])
         # self.__esJobManager.initAthenaMPProcess(job["AthenaMPCmd"])
         ret = self.__esJobManager.init(socketname='EventService_EventRanges', 
                                        context='local',
                                        athenaMPCmd=job["AthenaMPCmd"],
                                        tokenExtractorCmd=job.get("TokenExtractCmd",None))
         return True, None
      except:
         logger.exception("Rank %s: Failed to setup job",self.__rank)
         if self.__esJobManager is not None:
            self.__esJobManager.terminate()
         raise

   def getJob(self):
      request = {'Test':'TEST', 'rank': self.__rank}
      logger.debug("Rank %s: getJob(request: %s)",self.__rank, request)
      status, output = self.__comm.sendRequest('getJob',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
      if status:
         statusCode = output["StatusCode"]
         job = output["job"]
         if statusCode == 0 and job:
            return True, job
      return False, None

   def startStagerThread(self, job):
      logger.debug("Rank %s: initStagerThread: workdir: %s",self.__rank, os.getcwd())
      try:
         
         self.__stagerThread = DroidStager.DroidStager(self.__globalWorkingDir, self.__localWorkingDir, outputs=self.__outputs, job=job, esJobManager=self.__esJobManager, outputDir=self.__outputDir, rank=self.__rank)
         self.__stagerThread.start()
         return 0, None
      except:
         logger.exception("Rank %s: Failed to initStagerThread",self.__rank)
         return -1, str(traceback.format_exc())

   def stopStagerThread(self):
      logger.debug("Rank %s: stopStagerThread: workdir: %s",self.__rank, os.getcwd())
      self.__stagerThread.stop()
      logger.debug("Rank %s: waiting stager thread to finish",self.__rank)
      while not self.__stagerThread.isFinished():
         self.updateOutputs()
         time.sleep(1)
      logger.debug("Rank %s: stager thread finished",self.__rank)

   def getEventRanges(self, nRanges=1):
      #if self.__firstGetEventRanges:
      #   request = {'nRanges': self.__ATHENA_PROC_NUMBER}
      #   self.__firstGetEventRanges = False
      #else:
      #   request = {'nRanges': nRanges}
      request = {'jobId': self.__jobId, 'nRanges': nRanges}
      logger.debug("Rank %s: getEventRanges(request: %s)",self.__rank, request)
      status, output = self.__comm.sendRequest('getEventRanges',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
      if status:
         statusCode = output["StatusCode"]
         eventRanges = output['eventRanges']
         if statusCode == 0:
            return True, eventRanges
      return False, None

   def updateEventRange(self, output):
      try:
         eventRangeID = output.split(",")[1]
      except Exception, e:
         logger.exception("Rank %s: failed to get eventRangeID from output: %s",self.__rank, output)
         raise
      status, output = self.copyOutput(output)
      if status != 0:
         logger.debug("Rank %s: failed to copy output from local working dir to global working dir: %s",self.__rank, output)
         return False
      request = {"eventRangeID": eventRangeID,
               'eventStatus': "finished",
               "output": output}
      logger.debug("Rank %s: updateEventRange(request: %s)",self.__rank, request)
      retStatus, retOutput = self.__comm.sendRequest('updateEventRange',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, retStatus, retOutput)
      if retStatus:
         statusCode = retOutput["StatusCode"]
         if statusCode == 0:
            return True
      return False


   def dumpUpdates(self, outputs):
      timeNow = datetime.datetime.utcnow()
      outFileName = 'rank_' + str(self.__rank) + '_' + timeNow.strftime("%Y-%m-%d-%H-%M-%S") + '.dump'
      outFileName = os.path.join(self.globalWorkingDir, outFileName)
      outFile = open(outFileName,'w')
      for eventRangeID,status,output in outputs:
         outFile.write('{0} {1} {2}\n'.format(eventRangeID,status,output))
      outFile.close()

   def updatePandaEventRanges(self, event_ranges):
      """ Update an event range on the Event Server """
      logger.debug("Updating event ranges..")
      try:
         test = sys.modules['pUtil']
      except:
         logger.debug("loading pUtil")
      import pUtil
      
      message = ""
      #url = "https://aipanda007.cern.ch:25443/server/panda"
      url = "https://pandaserver.cern.ch:25443/server/panda"
      # eventRanges = [{'eventRangeID': '4001396-1800223966-4426028-1-2', 'eventStatus':'running'}, {'eventRangeID': '4001396-1800223966-4426028-2-2','eventStatus':'running'}]

      node={}
      node['eventRanges']=json.dumps(event_ranges)

      # open connection
      ret = pUtil.httpConnect(node, url, path='.', mode="UPDATEEVENTRANGES")
      # response = json.loads(ret[1])

      status = ret[0]
      if ret[0]: # non-zero return code
         message = "Failed to update event range - error code = %d, error: " % (ret[0], ret[1])
      else:
         response = json.loads(json.dumps(ret[1]))
         status = int(response['StatusCode'])
         message = json.dumps(response['Returns'])

      return status, message

   def updateOutputs(self, signal=False, final=False):
      outputs = []
      stagedOutpus = []
      while not self.__outputs.empty():
         output = self.__outputs.get()
         outputs.append(output)
         if output['eventStatus'] == 'stagedOut':
            stagedOutpus.append({'eventRangeID': output['eventRangeID'], 'eventStatus': 'finished', 'objstoreID': output['objstoreID']})
         elif output['eventStatus'].startswith("ERR") and self.__yodaToOS:
            stagedOutpus.append({'eventRangeID': output['eventRangeID'], 'eventStatus': 'failed'})
      if len(stagedOutpus):
         logger.debug("Rank %s: updatePandaEventRanges(request: %s)",self.__rank, stagedOutpus)
         retStatus, retOutput = self.updatePandaEventRanges(stagedOutpus)
         if retStatus == 0:
            logger.debug("Rank %s: updatePandaEventRanges(status: %s, output: %s)",self.__rank, retStatus, retOutput)
      if outputs:
         logger.debug("Rank %s: updateEventRanges(request: %s)",self.__rank, outputs)
         retStatus, retOutput = self.__comm.sendRequest('updateEventRanges',outputs)
         logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, retStatus, retOutput)

      return True

   def finishJob(self):
      if not self.__isFinished:
         request = {'jobId': self.__jobId, 'rank': self.__rank, 'state': 'finished'}
         logger.debug("Rank %s: finishJob(request: %s)",self.__rank, request)
         status, output = self.__comm.sendRequest('finishJob',request)
         logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
         if status:
            statusCode = output["StatusCode"]

         #self.__comm.disconnect()
         return True
      return False

   def failedJob(self):
      request = {'jobId': self.__jobId, 'rank': self.__rank, 'state': 'failed'}
      logger.debug("Rank %s: finishJob(request: %s)",self.__rank, request)
      status, output = self.__comm.sendRequest('finishJob',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
      if status:
         statusCode = output["StatusCode"]
         if statusCode == 0:
            return True
      return False

   def finishDroid(self):
      request = {'state': 'finished'}
      logger.debug("Rank %s: finishDroid(request: %s)",self.__rank, request)
      status, output = self.__comm.sendRequest('finishDroid',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
      if status:
         statusCode = output["StatusCode"]
         if statusCode == 0:
            return True
      self.__comm.disconnect()
      return False

   def heartbeat(self):
      request = self.getAccountingMetrics()
      self.__jobMetrics[self.__jobId] = request
      logger.debug("Rank %s: heartbeat(request: %s)",self.__rank, request)
      status, output = self.__comm.sendRequest('heartbeat',request)
      logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)

      self.dumpJobMetrics()

      if status:
         statusCode = output["StatusCode"]
         if statusCode == 0:
            return True
      return False

   def getAccountingMetrics(self):
      metrics = {}
      if self.__esJobManager:
         metrics = self.__esJobManager.getAccountingMetrics()
      metrics['jobId'] = self.__jobId
      metrics['rank'] = self.__rank
      if self.__startTimeOneJobDroid:
         metrics['totalTime'] =  time.time() - self.__startTimeOneJobDroid
      else:
         metrics['totalTime'] = 0
      processedEvents = metrics['processedEvents']
      if processedEvents < 1:
         processedEvents = 1

      metrics['avgTimePerEvent'] = metrics['totalTime'] * metrics['cores'] / processedEvents

      return metrics

   def dumpJobMetrics(self):
      jobMetricsFileName = "jobMetrics-rank_%s.json" % self.__rank
      outputDir = self.__currentDir
      jobMetrics = os.path.join(outputDir, jobMetricsFileName)
      logger.debug("JobMetrics file: %s",jobMetrics)
      tmpFile = open(jobMetrics, "w")
      json.dump(self.__jobMetrics, tmpFile)
      tmpFile.close()

   def pollYodaMessage(self):
      logger.debug("Rank %s: pollYodaMessage",self.__rank)
      if True:
         status, output = self.__comm.waitMessage()
         logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
         if status:
            statusCode = output["StatusCode"]
            state = output["State"]
            if statusCode == 0 and state == 'finished':
               return True
      return True

   def waitYoda(self):
      logger.debug("Rank %s: WaitYoda" % (self.__rank))
      while True:
         status, output = self.__comm.waitMessage()
         logger.debug("Rank %s: (status: %s, output: %s)",self.__rank, status, output)
         if status:
            statusCode = output["StatusCode"]
            state = output["State"]
            if statusCode == 0 and state == 'finished':
               return True
      return True

   def runOneJob(self):
      logger.info("Droid Starts to get job")
      status, job = self.getJob()
      logger.info("Rank %s: getJob(%s)",self.__rank, job)
      if not status or not job:
         logger.debug("Rank %s: Failed to get job",self.__rank)
         # self.failedJob()
         return -1
      status, output = self.setup(job)
      logger.info("Rank %s: setup job(status:%s, output:%s)",self.__rank, status, output)
      if not status:
         logger.debug("Rank %s: Failed to setup job(%s)",self.__rank, output)
         self.failedJob()
         return -1

      # main loop
      failedNum = 0
      #logger.info("Rank %s: isDead: %s" % (self.__rank, self.__esJobManager.isDead()))
      heartbeatTime = None
      logger.info("Rank %s: os.times: %s",self.__rank, os.times())
      while not self.__esJobManager.isDead():
         #logger.info("Rank %s: isDead: %s" % (self.__rank, self.__esJobManager.isDead()))
         #logger.info("Rank %s: isNeedMoreEvents: %s" % (self.__rank, self.__esJobManager.isNeedMoreEvents()))
         while self.__esJobManager.isNeedMoreEvents() > 0:
            neededEvents = self.__esJobManager.isNeedMoreEvents()
            logger.info("Rank %s: need %s events",self.__rank, neededEvents)
            status, eventRanges = self.getEventRanges(neededEvents)
            # failed to get message again and again
            if not status:
               failedNum += 1
               if failedNum > 30:
                  logger.warning("Rank %s: failed to get events more than 30 times. finish job",self.__rank)
                  self.__esJobManager.insertEventRange("No more events")
               else:
                  continue
            else:
               failedNum = 0
               logger.info("Rank %s: get event ranges(%s)",self.__rank, eventRanges)
               if len(eventRanges) == 0:
                  logger.info("Rank %s: no more events",self.__rank)
                  self.__esJobManager.insertEventRange("No more events")
               else:   
                  self.__esJobManager.insertEventRanges(eventRanges)

         self.__esJobManager.poll()
         self.updateOutputs()

         time.sleep(0.001)
         if heartbeatTime is None:
            self.heartbeat()
            heartbeatTime = time.time()
         elif time.time() - heartbeatTime > 60:
            self.heartbeat()
            logger.info("Rank %s: os.times: %s",self.__rank, os.times())
            heartbeatTime = time.time()

      self.heartbeat()
      self.__esJobManager.flushMessages()
      self.stopStagerThread()
      self.updateOutputs()

      logger.info("Rank %s: post exec job",self.__rank)
      self.postExecJob()
      self.heartbeat()
      logger.info("Rank %s: finish job",self.__rank)
      self.finishJob()
      #self.waitYoda()
      return self.__esJobManager.getChildRetStatus()

   def preCheck(self):
      if not os.access('/tmp', os.W_OK):
         logger.info("Rank %s: PreCheck /tmp is readonly",self.__rank)
         status, output = commands.getstatusoutput("ll /|grep tmp")
         logger.info("Rank %s: tmp dir: %s",self.__rank, output)
         return 1
      return 0

   def run(self):
      logger.info("Rank %s: Droid starts on %s",self.__rank, self.__hostname)
      if self.preCheck():
         logger.info("Rank %s: Droid failed preCheck, exit",self.__rank)
         return 1

      while not self.__stop:
         logger.info("Rank %s: Droid starts to run one job",self.__rank)
         os.chdir(self.__globalWorkingDir)
         try:
            ret = self.runOneJob()
            if ret != 0:
               logger.warning("Rank %s: Droid fails to run one job: ret - %s",self.__rank, ret)
               break
         except:
            logger.exception("Rank %s: Droid throws exception when running one job: %s",self.__rank)
            break
         os.chdir(self.__globalWorkingDir)
         logger.info("Rank %s: Droid finishes to run one job",self.__rank)
      self.finishDroid()
      return 0
         
   def stop(self, signum=None, frame=None):
      logger.info('Rank %s: stop signal %s received',self.__rank, signum)
      self.__stop = True
      signal_block.block_sig(signum)
      signal.siginterrupt(signum, False)
      if self.__esJobManager:
         self.__esJobManager.terminate()
      self.getAccountingMetrics()
      self.dumpJobMetrics()
      self.heartbeat()
      #self.__esJobManager.terminate()
      self.__esJobManager.flushMessages()
      self.updateOutputs(signal=True, final=True)

      logger.info("Rank %s: post exec job",self.__rank)
      self.postExecJob()
      #logger.info("Rank %s: finish job" % self.__rank)
      #self.finishJob()

      logger.info('Rank %s: stop',self.__rank)
      #signal.siginterrupt(signum, True)
      signal_block.unblock_sig(signum)
      #sys.exit(0)

   def __del_not_use__(self):
      logger.info('Rank %s: __del__ function',self.__rank)
      #self.__esJobManager.terminate()
      #self.__esJobManager.flushMessages()
      #output = self.__esJobManager.getOutput()
      #while output:
      #   logger.info("Rank %s: get output(%s)" % (self.__rank, output))
      #   self.updateEventRange(output)
      #   output = self.__esJobManager.getOutput()

      #logger.info("Rank %s: post exec job" % self.__rank)
      #self.postExecJob()
      self.__esJobManager.flushMessages()
      self.updateOutputs(signal=True, final=True)

      logger.info("Rank %s: post exec job",self.__rank)
      self.postExecJob()
      logger.info("Rank %s: finish job",self.__rank)
      self.finishJob()

      logger.info('Rank %s: __del__ function',self.__rank)


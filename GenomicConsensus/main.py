#!/usr/bin/env python
#################################################################################
# Copyright (c) 2011-2013, Pacific Biosciences of California, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
# * Neither the name of Pacific Biosciences nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE.  THIS SOFTWARE IS PROVIDED BY PACIFIC BIOSCIENCES AND ITS
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL PACIFIC BIOSCIENCES OR
# ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#################################################################################

# Author: David Alexander

from __future__ import absolute_import

import argparse, atexit, cProfile, gc, glob, h5py, logging, multiprocessing
import os, pstats, random, shutil, tempfile, time, threading, Queue, traceback
import re
import sys

import pysam

from pbcommand.utils import setup_log
from pbcommand.cli import pbparser_runner
from pbcore.io import AlignmentSet, ContigSet

from GenomicConsensus import reference
from GenomicConsensus.options import (options, Constants,
                                      get_parser,
                                      processOptions,
                                      resolveOptions,
                                      consensusCoreVersion,
                                      consensusCore2Version)
from GenomicConsensus.utils import (IncompatibleDataException,
                                    datasetCountExceedsThreshold,
                                    die)

class ToolRunner(object):
    """
    The main driver class for the GenomicConsensus tool.  It is assumed that
    arguments have already been parsed and used to populate the global
    'options' namespace before instantiating this class.
    """
    def __init__(self):
        self._inAlnFile = None
        self._resultsQueue = None
        self._workQueue = None
        self._slaves = None
        self._algorithm = None
        self._algorithmConfiguration = None
        self._aborting = False

    def _setupLogging(self):
        if options.quiet:
            logLevel = logging.ERROR
        elif options.verbosity >= 2:
            logLevel = logging.DEBUG
        elif options.verbosity == 1:
            logLevel = logging.INFO
        else:
            logLevel = logging.WARNING
        log = logging.getLogger()
        log.setLevel(logLevel)

    def _makeTemporaryDirectory(self):
        """
        Make a temp dir where we can stash things if necessary.
        """
        options.temporaryDirectory = tempfile.mkdtemp(prefix="GenomicConsensus-", dir="/tmp")
        logging.info("Created temporary directory %s" % (options.temporaryDirectory,) )

    def _algorithmByName(self, name):
        if name == "plurality":
            from GenomicConsensus.plurality import plurality
            algo = plurality
        elif name == "quiver":
            from GenomicConsensus.quiver import quiver
            algo = quiver
        elif name == "arrow":
            from GenomicConsensus.arrow import arrow
            algo = arrow
        else:
            die("Failure: unrecognized algorithm %s" % name)
        isOK, msg = algo.availability
        if not isOK:
            die("Failure: %s" % msg)
        return algo

    def _launchSlaves(self):
        """
        Launch a group of worker processes (self._slaves), the queue
        (self._workQueue) that will be used to send them chunks of
        work, and the queue that will be used to receive back the
        results (self._resultsQueue).

        Additionally, launch the result collector process.
        """
        availableCpus = multiprocessing.cpu_count()
        logging.info("Available CPUs: %d" % (availableCpus,))
        logging.info("Requested workers: %d" % (options.numWorkers,))
        logging.info("Parallel Mode: %s" % ("Threaded" if options.threaded else "Process",))
        if (options.numWorkers > availableCpus):
            logging.warn("More workers requested (%d) than CPUs available (%d);"
                         " may result in suboptimal performance."
                         % (options.numWorkers, availableCpus))
        self._initQueues()

        WorkerType, ResultCollectorType = self._algorithm.slaveFactories(options.threaded)
        self._slaves = []
        for i in xrange(options.numWorkers):
            p = WorkerType(self._workQueue, self._resultsQueue, self._algorithmConfiguration)
            self._slaves.append(p)
            p.start()
        logging.info("Launched compute slaves.")

        rcp = ResultCollectorType(self._resultsQueue, self._algorithmConfiguration)
        rcp.start()
        self._slaves.append(rcp)
        logging.info("Launched collector slave.")

    def _initQueues(self):
        if options.threaded:
            self._workQueue = Queue.Queue(options.queueSize)
            self._resultsQueue = Queue.Queue(options.queueSize)
        else:
            self._workQueue = multiprocessing.Queue(options.queueSize)
            self._resultsQueue = multiprocessing.Queue(options.queueSize)

    def _readAlignmentInput(self):
        """
        Read the AlignmentSet input file and
        store it as self._inAlnFile.
        """
        fname = options.inputFilename
        self._inAlnFile = AlignmentSet(fname)

    def _loadReference(self, alnFile):
        logging.info("Loading reference")
        err = reference.loadFromFile(options.referenceFilename, alnFile)
        if err:
            die("Error loading reference")
        # Grok the referenceWindow spec, if any.
        if options.referenceWindowsAsString is None:
            options.referenceWindows = ()
        elif options.skipUnrecognizedContigs:
            # This is a workaround for smrtpipe scatter/gather.
            options.referenceWindows = []
            for s in options.referenceWindowsAsString.split(","):
                try:
                    win = reference.stringToWindow(s)
                    options.referenceWindows.append(win)
                except:
                    pass
        else:
            options.referenceWindows = map(reference.stringToWindow,
                                           options.referenceWindowsAsString.split(","))
        if options.referenceWindowsFromAlignment:
            options.referenceWindows = alnFile.refWindows

    def _checkFileCompatibility(self, alnFile):
        if not alnFile.isSorted:
            die("Input Alignment file must be sorted.")
        if alnFile.isEmpty:
            die("Input Alignment file must be nonempty.")

    def _shouldDisableChunkCache(self, alnFile):
        #if isinstance(alnFile, CmpH5Reader):
        #if alnFile.isCmpH5:
        #    threshold = options.autoDisableHdf5ChunkCache
        #    return datasetCountExceedsThreshold(alnFile, threshold)
        #else:
        #    return False
        return True

    def _configureAlgorithm(self, options, alnFile):
        assert self._algorithm != None
        try:
            self._algorithmConfiguration = self._algorithm.configure(options, alnFile)
        except IncompatibleDataException as e:
            die("Failure: %s" % e.message)

    def _mainLoop(self):
        # Split up reference genome into chunks and farm out the
        # a chunk as a unit of work.
        logging.debug("Starting main loop.")
        ids = reference.enumerateIds(options.referenceWindows)
        for _id in ids:
            if options.fancyChunking:
                chunks = reference.fancyEnumerateChunks(self._inAlnFile,
                                                        _id,
                                                        options.referenceChunkSize,
                                                        options.minCoverage,
                                                        options.minMapQV,
                                                        options.referenceWindows)
            else:
                chunks = reference.enumerateChunks(_id,
                                                   options.referenceChunkSize,
                                                   options.referenceWindows)
            for chunk in chunks:
                if self._aborting: return
                self._workQueue.put(chunk)

        # Write sentinels ("end-of-work-stream")
        for i in xrange(options.numWorkers):
            self._workQueue.put(None)

    def _printProfiles(self):
        for profile in glob.glob(os.path.join(options.temporaryDirectory, "*")):
            pstats.Stats(profile).sort_stats("time").print_stats(20)

    def _cleanup(self):
        if options.doProfiling:
            logging.info("Removing %s" % options.temporaryDirectory)
            shutil.rmtree(options.temporaryDirectory, ignore_errors=True)

    def _setupEvidenceDumpDirectory(self, directoryName):
        if os.path.exists(directoryName):
            shutil.rmtree(directoryName)
        os.makedirs(directoryName)

    @property
    def aborting(self):
        return self._aborting

    def abortWork(self, why):
        """
        Performs a shutdown of all the slave processes.  Called by the
        monitoring thread when a child process exits with a non-zero,
        or when a keyboard interrupt (Ctrl-C) is given. Not called
        during normal shutdown.
        """
        logging.error(why)
        self._aborting = True
        self._resultsQueue.close()
        self._workQueue.close()

    @property
    def slaves(self):
        return self._slaves

    def main(self):

        # This looks scary but it's not.  Python uses reference
        # counting and has a secondary, optional garbage collector for
        # collecting garbage cycles.  Unfortunately when a cyclic GC
        # happens when a thread is calling cPickle.dumps, the
        # interpreter crashes sometimes.  See Bug 19704.  Since we
        # don't leak garbage cycles, disabling the cyclic GC is
        # essentially harmless.
        gc.disable()

        self._algorithm = self._algorithmByName(options.algorithm)
        self._setupLogging()
        random.seed(42)

        logging.info("h5py version: %s" % h5py.version.version)
        logging.info("hdf5 version: %s" % h5py.version.hdf5_version)
        logging.info("ConsensusCore version: %s" %
                     (consensusCoreVersion() or "ConsensusCore unavailable"))
        logging.info("ConsensusCore2 version: %s" %
                     (consensusCore2Version() or "ConsensusCore2 unavailable"))
        logging.info("Starting.")

        atexit.register(self._cleanup)
        if options.doProfiling:
            self._makeTemporaryDirectory()

        with AlignmentSet(options.inputFilename) as peekFile:
            if options.algorithm == "arrow" and peekFile.isCmpH5:
                die("Arrow does not support CmpH5 files")
            if not peekFile.isCmpH5 and not peekFile.hasPbi:
                die("Genomic Consensus only works with cmp.h5 files and BAM "
                    "files with accompanying .pbi files")
            logging.info("Peeking at file %s" % options.inputFilename)
            logging.info("Input data: numAlnHits=%d" % len(peekFile))
            resolveOptions(peekFile)
            self._loadReference(peekFile)
            self._checkFileCompatibility(peekFile)
            self._configureAlgorithm(options, peekFile)
            options.disableHdf5ChunkCache = True
            #options.disableHdf5ChunkCache = self._shouldDisableChunkCache(peekFile)
            #if options.disableHdf5ChunkCache:
            #    logging.info("Will disable HDF5 chunk cache (large number of datasets)")
            #logging.debug("After peek, # hdf5 objects open: %d" % h5py.h5f.get_obj_count())

        if options.dumpEvidence:
            self._setupEvidenceDumpDirectory(options.evidenceDirectory)

        self._launchSlaves()
        self._readAlignmentInput()

        monitoringThread = threading.Thread(target=monitorSlaves, args=(self,))
        monitoringThread.start()

        try:
            if options.doProfiling:
                cProfile.runctx("self._mainLoop()",
                                globals=globals(),
                                locals=locals(),
                                filename=os.path.join(options.temporaryDirectory,
                                                      "profile-main.out"))

            elif options.debug:
                if not options.threaded:
                    die("Debugging only works with -T (threaded) mode")
                logging.info("PID: %d", os.getpid())
                import ipdb
                with ipdb.launch_ipdb_on_exception():
                    self._mainLoop()

            else:
                self._mainLoop()
        except:
            why = traceback.format_exc()
            self.abortWork(why)

        monitoringThread.join()

        if self._aborting:
            logging.error("Aborting")
            return -1
        else:
            logging.info("Finished.")

        if options.doProfiling:
            self._printProfiles()

        # close h5 file.
        self._inAlnFile.close()
        return 0

def monitorSlaves(driver):
    """
    Promptly aborts if a child is found to have exited with a nonzero
    exit code received; otherwise returns when all processes exit cleanly (0).

    This approach is portable--catching SIGCHLD doesn't work on
    Windows.
    """
    while not driver.aborting:
        all_exited = all(not p.is_alive() for p in driver.slaves)
        nonzero_exits = [p.exitcode for p in driver.slaves if p.exitcode]
        if nonzero_exits:
            exitcode = nonzero_exits[0]
            driver.abortWork("Child process exited with exitcode=%d.  Aborting." % exitcode)
            return exitcode
        elif all_exited:
            return 0
        time.sleep(1)

def args_runner(args):
    options.__dict__.update(args.__dict__)
    processOptions()
    tr = ToolRunner()
    return tr.main()

def resolved_tool_contract_runner(resolved_contract):
    rc = resolved_contract
    alignment_path = rc.task.input_files[0]
    reference_path = rc.task.input_files[1]
    gff_path = rc.task.output_files[0]
    dataset_path = rc.task.output_files[1]
    fasta_path = re.sub(".contigset.xml", ".fasta", dataset_path)
    fastq_path = rc.task.output_files[2]
    args = [
        alignment_path,
        "--reference", reference_path,
        "--outputFilename", gff_path,
        "--outputFilename", fasta_path,
        "--outputFilename", fastq_path,
        "--numWorkers", str(rc.task.nproc),
        "--minCoverage", str(rc.task.options[Constants.MIN_COVERAGE_ID]),
        "--minConfidence", str(rc.task.options[Constants.MIN_CONFIDENCE_ID]),
        "--algorithm", rc.task.options[Constants.ALGORITHM_ID],
        "--alignmentSetRefWindows",
    ]
    if rc.task.options[Constants.DIPLOID_MODE_ID]:
        args.append("--diploid")
    args_ = get_parser().arg_parser.parser.parse_args(args)
    rc = args_runner(args_)
    if rc == 0:
        pysam.faidx(fasta_path)
        ds = ContigSet(fasta_path, strict=True)
        ds.write(dataset_path)
    return rc

def main(argv=sys.argv):
    logFormat = '[%(levelname)s] %(message)s'
    logging.basicConfig(level=logging.WARN, format=logFormat)
    log = logging.getLogger()
    def dummy_setup(*args, **kwargs):
        pass
    return pbparser_runner(
        argv=argv[1:],
        parser=get_parser(),
        args_runner_func=args_runner,
        contract_runner_func=resolved_tool_contract_runner,
        alog=log,
        setup_log_func=dummy_setup)

if __name__ == "__main__":
    sys.exit(main(sys.argv))

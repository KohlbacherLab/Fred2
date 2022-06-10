# This code is part of the epytope distribution and governed by its
# license.  Please see the LICENSE file that should have been included
# as part of this package.
"""
.. module:: EpitopePrediction.ANN
   :synopsis: This module contains all classes for ANN-based epitope prediction methods.
.. moduleauthor:: schubert, walzer

"""
import abc

import itertools
import warnings
import logging
import pandas
import subprocess
import csv
import os
import math
import re

from collections import defaultdict
from enum import IntEnum

from epytope.Core.Allele import Allele, CombinedAllele, MouseAllele
from epytope.Core.Peptide import Peptide
from epytope.Core.Result import EpitopePredictionResult
from epytope.Core.Base import AEpitopePrediction, AExternal
from tempfile import NamedTemporaryFile, mkstemp

class AExternalEpitopePrediction(AEpitopePrediction, AExternal):
    """
        Abstract class representing an external prediction function. Implementations shall wrap external binaries by
        following the given abstraction.
    """

    @abc.abstractmethod
    def prepare_input(self, input, file):
        """
        Prepares input for external tools
        and writes them to _file in the specific format

        NO return value!

        :param: list(str) _input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        return NotImplementedError

    def predict(self, peptides, alleles=None, command=None, options=None, **kwargs):
        """
        Overwrites AEpitopePrediction.predict

        :param peptides: A list of or a single :class:`~epytope.Core.Peptide.Peptide` object
        :type peptides: list(:class:`~epytope.Core.Peptide.Peptide`) or :class:`~epytope.Core.Peptide.Peptide`
        :param alleles: A list of or a single :class:`~epytope.Core.Allele.Allele` object. If no
                        :class:`~epytope.Core.Allele.Allele` are provided, predictions are made for all
                        :class:`~epytope.Core.Allele.Allele` supported by the prediction method
        :type alleles: list(:class:`~epytope.Core.Allele.Allele`)/:class:`~epytope.Core.Allele.Allele`
        :param str command: The path to a alternative binary (can be used if binary is not globally executable)
        :param str options: A string of additional options directly past to the external tool.
        :keyword chunksize: denotes the chunksize in which the number of peptides are bulk processed
        :return: A :class:`~epytope.Core.Result.EpitopePredictionResult` object
        :rtype: :class:`~epytope.Core.Result.EpitopePredictionResult`
        """
        if not self.is_in_path() and command is None:
            raise RuntimeError("{name} {version} could not be found in PATH".format(name=self.name,
                                                                                    version=self.version))
        external_version = self.get_external_version(path=command)
        if self.version != external_version and external_version is not None:
            raise RuntimeError("Internal version {internal_version} does "
                               "not match external version {external_version}".format(internal_version=self.version,
                                                                                      external_version=external_version))

        if isinstance(peptides, Peptide):
            pep_seqs = {str(peptides): peptides}
        else:
            pep_seqs = {}
            for p in peptides:
                if not isinstance(p, Peptide):
                    raise ValueError("Input is not of type Protein or Peptide")
                pep_seqs[str(p)] = p

        chunksize = len(pep_seqs)
        if 'chunks' in kwargs:
            chunksize = kwargs['chunks']

        if alleles is None:
            alleles = [Allele(a) for a in self.supportedAlleles]
        else:
            if isinstance(alleles, Allele):
                alleles = [alleles]
            if any(not isinstance(p, Allele) for p in alleles):
                raise ValueError("Input is not of type Allele")

        # Create dictionary containing the predictors string representation and the Allele Obj representation of the allele
        alleles_string = {conv_a: a for conv_a, a in zip(self.convert_alleles(alleles), alleles)}
        
        # Create empty result dictionary to fill downstream
        result = {}

        # group alleles in blocks of 80 alleles (NetMHC can't deal with more)
        _MAX_ALLELES = 50

        # allow custom executable specification
        if command is not None:
            exe = self.command.split()[0]
            _command = self.command.replace(exe, command)
        else:
            _command = self.command

        allele_groups = []
        c_a = 0
        allele_group = []
        for a in alleles_string.keys():
            if c_a >= _MAX_ALLELES:
                c_a = 0
                allele_groups.append(allele_group)
                if str(alleles_string[a]) not in self.supportedAlleles:
                    logging.warning("Allele %s is not supported by %s" % (str(alleles_string[a]), self.name))
                    allele_group = []
                    continue
                allele_group = [a]
            else:
                if str(alleles_string[a]) not in self.supportedAlleles:
                    logging.warning("Allele %s is not supported by %s" % (str(alleles_string[a]), self.name))
                    continue
                allele_group.append(a)
                c_a += 1

        if len(allele_group) > 0:
            allele_groups.append(allele_group)
        # export peptides to peptide list

        pep_groups = list(pep_seqs.keys())
        pep_groups.sort(key=len)
        for length, peps in itertools.groupby(pep_groups, key=len):
            if length not in self.supportedLength:
                logging.warning("Peptide length must be at least %i or at most %i for %s but is %i" % (min(self.supportedLength), max(self.supportedLength),
                                                                                       self.name, length))
                continue
            peps = list(peps)
            
            for i in range(0, len(peps), chunksize):
                # Create a temporary file for subprocess to write to. The
                # handle is not needed on the python end, as only the path will
                # be passed to the subprocess.
                _, tmp_out_path = mkstemp()
                # Create a temporary file to be used for the peptide input
                tmp_file = NamedTemporaryFile(mode="r+", delete=False)
                self.prepare_input(peps[i:i+chunksize], tmp_file)
                tmp_file.close()

                # generate cmd command
                for allele_group in allele_groups:
                    try:
                        stdo = None
                        stde = None
                        cmd = _command.format(peptides=tmp_file.name, alleles=",".join(allele_group),
                                              options="" if options is None else options, out=tmp_out_path, length=str(length))
                        p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT)
                        stdo, stde = p.communicate()
                        stdr = p.returncode
                        if stdr > 0:
                            raise RuntimeError("Unsuccessful execution of " + cmd + " (EXIT!=0) with output:\n" + stdo.decode())
                        if os.path.getsize(tmp_out_path) == 0:
                            raise RuntimeError("Unsuccessful execution of " + cmd + " (empty output file) with output:\n" + stdo.decode())
                    except Exception as e:
                        raise RuntimeError(e)

                    # Obtain parsed output dataframe containing the peptide scores and/or ranks
                    res_tmp = self.parse_external_result(tmp_out_path)
                    for allele_string, scores in res_tmp.items():
                        allele = alleles_string[allele_string]
                        if allele not in result.keys():
                            result[allele] = {}
                        for scoretype, pep_scores in scores.items():
                            if scoretype not in result[allele].keys():
                                result[allele][scoretype] = {}
                            for pep, score in pep_scores.items():
                                result[allele][scoretype][pep_seqs[pep]] = score
                                
                os.remove(tmp_file.name)
                os.remove(tmp_out_path)
        
        if not result:
            raise ValueError("No predictions could be made with " + self.name +
                             " for given input. Check your epitope length and HLA allele combination.")
        
        df_result = EpitopePredictionResult.from_dict(result, list(pep_seqs.values()), self.name)

        return df_result


class NetMHC_3_4(AExternalEpitopePrediction):
    """
    Implements the NetMHC binding (in current form for netMHC3.4).

    .. note::

        NetMHC-3.0: accurate web accessible predictions of human, mouse and monkey MHC class I affinities for peptides
        of length 8-11. Lundegaard C, Lamberth K, Harndahl M, Buus S, Lund O, Nielsen M.
        Nucleic Acids Res. 1;36(Web Server issue):W509-12. 2008

        Accurate approximation method for prediction of class I MHC affinities for peptides of length 8, 10 and 11 using
        prediction tools trained on 9mers. Lundegaard C, Lund O, Nielsen M. Bioinformatics, 24(11):1397-98, 2008.

    """

    __alleles = frozenset(['HLA-A*01:01', 'HLA-A*02:01', 'HLA-A*02:02', 'HLA-A*02:03', 'HLA-A*02:06', 'HLA-A*02:11', 'HLA-A*02:12', 'HLA-A*02:16',
                           'HLA-A*02:17', 'HLA-A*02:19', 'HLA-A*02:50', 'HLA-A*03:01', 'HLA-A*11:01', 'HLA-A*23:01', 'HLA-A*24:02', 'HLA-A*24:03',
                           'HLA-A*25:01', 'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*29:02', 'HLA-A*30:01', 'HLA-A*30:02', 'HLA-A*31:01',
                           'HLA-A*32:01', 'HLA-A*32:07', 'HLA-A*32:15', 'HLA-A*33:01', 'HLA-A*66:01', 'HLA-A*68:01', 'HLA-A*68:02', 'HLA-A*68:23',
                           'HLA-A*69:01', 'HLA-A*80:01', 'HLA-B*07:02', 'HLA-B*08:01', 'HLA-B*08:02', 'HLA-B*08:03', 'HLA-B*14:02', 'HLA-B*15:01',
                           'HLA-B*15:02', 'HLA-B*15:03', 'HLA-B*15:09', 'HLA-B*15:17', 'HLA-B*18:01', 'HLA-B*27:05', 'HLA-B*27:20', 'HLA-B*35:01',
                           'HLA-B*35:03', 'HLA-B*38:01', 'HLA-B*39:01', 'HLA-B*40:01', 'HLA-B*40:02', 'HLA-B*40:13', 'HLA-B*42:01', 'HLA-B*44:02',
                           'HLA-B*44:03', 'HLA-B*45:01', 'HLA-B*46:01', 'HLA-B*48:01', 'HLA-B*51:01', 'HLA-B*53:01', 'HLA-B*54:01', 'HLA-B*57:01',
                           'HLA-B*58:01', 'HLA-B*73:01', 'HLA-B*83:01', 'HLA-C*03:03', 'HLA-C*04:01', 'HLA-C*05:01', 'HLA-C*06:02', 'HLA-C*07:01',
                           'HLA-C*07:02', 'HLA-C*08:02', 'HLA-C*12:03', 'HLA-C*14:02', 'HLA-C*15:02', 'HLA-E*01:01',
                           'H-2-Db', 'H-2-Dd', 'H-2-Kb', 'H-2-Kd', 'H-2-Kk', 'H-2-Ld'])
    __supported_length = frozenset([8, 9, 10, 11])
    __name = "netmhc"
    __command = "netMHC -p {peptides} -a {alleles} -x {out} {options}"
    __version = "3.4"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s:%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    @property
    def supportedAlleles(self):
        """
        A list of valid allele models
        """
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results 
        :rtype: dict
        """
        result = defaultdict(defaultdict)
        f = csv.reader(open(file, "r"), delimiter='\t')
        next(f)
        next(f)
        alleles = [x.split()[0] for x in f.next()[3:]]
        for l in f:
            if not l:
                continue
            pep_seq = l[PeptideIndex.NETMHC_3_4]
            for ic_50, a in zip(l[ScoreIndex.NETMHC_3_0:], alleles):
                sc = 1.0 - math.log(float(ic_50), 50000)
                result[a][pep_seq] = sc if sc > 0.0 else 0.0
        return dict(result)

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: dict
        """
        return super(NetMHC_3_4, self).get_external_version()

    def prepare_input(self, input, file):
        """
        Prepares input for external tools
        and writes them to file in the specific format

        NO return value!

        :param: list(str) input: The : sequences to write into _file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))


class NetMHC_3_0(NetMHC_3_4):
    """
    Implements the NetMHC binding (for netMHC3.0)::


    .. note::

        NetMHC-3.0: accurate web accessible predictions of human, mouse and monkey MHC class I affinities for peptides
        of length 8-11. Lundegaard C, Lamberth K, Harndahl M, Buus S, Lund O, Nielsen M.
        Nucleic Acids Res. 1;36(Web Server issue):W509-12. 2008

        Accurate approximation method for prediction of class I MHC affinities for peptides of length 8, 10 and 11
        using prediction tools trained on 9mers. Lundegaard C, Lund O, Nielsen M. Bioinformatics, 24(11):1397-98, 2008.
    """

    __alleles = frozenset(['HLA-A*01:01', 'HLA-A*02:01', 'HLA-A*02:02', 'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:06', 'HLA-A*02:11', 'HLA-A*02:12',
                           'HLA-A*02:16', 'HLA-A*02:19', 'HLA-A*03:01', 'HLA-A*11:01', 'HLA-A*23:01', 'HLA-A*24:02', 'HLA-A*24:03', 'HLA-A*26:01',
                           'HLA-A*26:02', 'HLA-A*29:02', 'HLA-A*30:01', 'HLA-A*30:02', 'HLA-A*31:01', 'HLA-A*33:01', 'HLA-A*68:01', 'HLA-A*68:02',
                           'HLA-A*69:01', 'HLA-B*07:02', 'HLA-B*08:01', 'HLA-B*08:02', 'HLA-B*15:01', 'HLA-B*18:01', 'HLA-B*27:05', 'HLA-B*35:01',
                           'HLA-B*39:01', 'HLA-B*40:01', 'HLA-B*40:02', 'HLA-B*44:02', 'HLA-B*44:03', 'HLA-B*45:01', 'HLA-B*51:01', 'HLA-B*53:01',
                           'HLA-B*54:01', 'HLA-B*57:01', 'HLA-B*58:01',
                           'H-2-Db', 'H-2-Dd', 'H-2-Kb', 'H-2-Kd', 'H-2-Kk', 'H-2-Ld'])  # no PSSM predictors

    __supported_length = frozenset([8, 9, 10, 11])
    __name = "netmhc"
    __version = "3.0a"
    __command = "netMHC-3.0 -p {peptides} -a {alleles} -x {out} -l {length} {options}"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedAlleles(self):
        """
        A list of valid :class:`~epytope.Core.Allele.Allele` models
        """
        return self.__alleles

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        result = defaultdict(dict)
        with open(file, 'r') as f:
            next(f, None)  # skip first line with logging stuff
            next(f, None)  # skip first line with nothing
            csvr = csv.reader(f, delimiter='\t')
            alleles = [x.split()[0] for x in csvr.next()[3:]]
            for l in csvr:
                if not l:
                    continue
                pep_seq = l[PeptideIndex.NETMHC_3_0]
                for ic_50, a in zip(l[ScoreIndex.NETMHC_3_0:], alleles):
                    sc = 1.0 - math.log(float(ic_50), 50000)
                    result[a][pep_seq] = sc if sc > 0.0 else 0.0
        if 'Average' in result:
            result.pop('Average')
        return dict(result)


class NetMHC_4_0(NetMHC_3_4):
    """
    Implements the NetMHC 4.0 binding

    .. note::
        Andreatta M, Nielsen M. Gapped sequence alignment using artificial neural networks:
        application to the MHC class I system. Bioinformatics (2016) Feb 15;32(4):511-7
    """
    __command = "netMHC -p {peptides} -a {alleles} -xls -xlsfile {out} {options}"
    __version = "4.0"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        f = csv.reader(open(file, "r"), delimiter='\t')
        alleles = [x.split()[0] for x in [x for x in next(f) if x.strip() != ""]]
        next(f)
        for l in f:
            if not l:
                continue
            pep_seq = l[PeptideIndex.NETMHC_4_0]
            for i, a in enumerate(alleles):
                ic_50 = l[(i+1) * Offset.NETMHC_4_0]
                sc = 1.0 - math.log(float(ic_50), 50000)
                rank = l[(i+1)* Offset.NETMHC_4_0 + 1]
                scores[a][pep_seq] = sc if sc > 0.0 else 0.0
                ranks[a][pep_seq] = float(rank)
        
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        # can not be determined netmhcpan does not support --version or similar
        return None


class NetMHCpan_2_4(AExternalEpitopePrediction):
    """
    Implements the NetMHC binding (in current form for netMHCpan 2.4).
    Supported  MHC alleles currently only restricted to HLA alleles.

    .. note::

        Nielsen, Morten, et al. "NetMHCpan, a method for quantitative predictions of peptide binding to any HLA-A and-B
        locus protein of known sequence." PloS one 2.8 (2007): e796.
    """
    __supported_length = frozenset([8, 9, 10, 11])
    __name = "netmhcpan"
    __command = "netMHCpan-2.4 -p {peptides} -a {alleles} {options} -ic50 -xls -xlsfile {out}"
    __alleles = frozenset(
        ['HLA-A*01:01', 'HLA-A*01:02', 'HLA-A*01:03', 'HLA-A*01:06', 'HLA-A*01:07', 'HLA-A*01:08', 'HLA-A*01:09', 'HLA-A*01:10', 'HLA-A*01:12',
         'HLA-A*01:13', 'HLA-A*01:14', 'HLA-A*01:17', 'HLA-A*01:19', 'HLA-A*01:20', 'HLA-A*01:21', 'HLA-A*01:23', 'HLA-A*01:24', 'HLA-A*01:25',
         'HLA-A*01:26', 'HLA-A*01:28', 'HLA-A*01:29', 'HLA-A*01:30', 'HLA-A*01:32', 'HLA-A*01:33', 'HLA-A*01:35', 'HLA-A*01:36', 'HLA-A*01:37',
         'HLA-A*01:38', 'HLA-A*01:39', 'HLA-A*01:40', 'HLA-A*01:41', 'HLA-A*01:42', 'HLA-A*01:43', 'HLA-A*01:44', 'HLA-A*01:45', 'HLA-A*01:46',
         'HLA-A*01:47', 'HLA-A*01:48', 'HLA-A*01:49', 'HLA-A*01:50', 'HLA-A*01:51', 'HLA-A*01:54', 'HLA-A*01:55', 'HLA-A*01:58', 'HLA-A*01:59',
         'HLA-A*01:60', 'HLA-A*01:61', 'HLA-A*01:62', 'HLA-A*01:63', 'HLA-A*01:64', 'HLA-A*01:65', 'HLA-A*01:66', 'HLA-A*02:01', 'HLA-A*02:02',
         'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:05', 'HLA-A*02:06', 'HLA-A*02:07', 'HLA-A*02:08', 'HLA-A*02:09', 'HLA-A*02:10', 'HLA-A*02:101',
         'HLA-A*02:102', 'HLA-A*02:103', 'HLA-A*02:104', 'HLA-A*02:105', 'HLA-A*02:106', 'HLA-A*02:107', 'HLA-A*02:108', 'HLA-A*02:109',
         'HLA-A*02:11', 'HLA-A*02:110', 'HLA-A*02:111', 'HLA-A*02:112', 'HLA-A*02:114', 'HLA-A*02:115', 'HLA-A*02:116', 'HLA-A*02:117',
         'HLA-A*02:118', 'HLA-A*02:119', 'HLA-A*02:12', 'HLA-A*02:120', 'HLA-A*02:121', 'HLA-A*02:122', 'HLA-A*02:123', 'HLA-A*02:124',
         'HLA-A*02:126', 'HLA-A*02:127', 'HLA-A*02:128', 'HLA-A*02:129', 'HLA-A*02:13', 'HLA-A*02:130', 'HLA-A*02:131', 'HLA-A*02:132',
         'HLA-A*02:133', 'HLA-A*02:134', 'HLA-A*02:135', 'HLA-A*02:136', 'HLA-A*02:137', 'HLA-A*02:138', 'HLA-A*02:139', 'HLA-A*02:14',
         'HLA-A*02:140', 'HLA-A*02:141', 'HLA-A*02:142', 'HLA-A*02:143', 'HLA-A*02:144', 'HLA-A*02:145', 'HLA-A*02:146', 'HLA-A*02:147',
         'HLA-A*02:148', 'HLA-A*02:149', 'HLA-A*02:150', 'HLA-A*02:151', 'HLA-A*02:152', 'HLA-A*02:153', 'HLA-A*02:154', 'HLA-A*02:155',
         'HLA-A*02:156', 'HLA-A*02:157', 'HLA-A*02:158', 'HLA-A*02:159', 'HLA-A*02:16', 'HLA-A*02:160', 'HLA-A*02:161', 'HLA-A*02:162',
         'HLA-A*02:163', 'HLA-A*02:164', 'HLA-A*02:165', 'HLA-A*02:166', 'HLA-A*02:167', 'HLA-A*02:168', 'HLA-A*02:169', 'HLA-A*02:17',
         'HLA-A*02:170', 'HLA-A*02:171', 'HLA-A*02:172', 'HLA-A*02:173', 'HLA-A*02:174', 'HLA-A*02:175', 'HLA-A*02:176', 'HLA-A*02:177',
         'HLA-A*02:178', 'HLA-A*02:179', 'HLA-A*02:18', 'HLA-A*02:180', 'HLA-A*02:181', 'HLA-A*02:182', 'HLA-A*02:183', 'HLA-A*02:184',
         'HLA-A*02:185', 'HLA-A*02:186', 'HLA-A*02:187', 'HLA-A*02:188', 'HLA-A*02:189', 'HLA-A*02:19', 'HLA-A*02:190', 'HLA-A*02:191',
         'HLA-A*02:192', 'HLA-A*02:193', 'HLA-A*02:194', 'HLA-A*02:195', 'HLA-A*02:196', 'HLA-A*02:197', 'HLA-A*02:198', 'HLA-A*02:199',
         'HLA-A*02:20', 'HLA-A*02:200', 'HLA-A*02:201', 'HLA-A*02:202', 'HLA-A*02:203', 'HLA-A*02:204', 'HLA-A*02:205', 'HLA-A*02:206',
         'HLA-A*02:207', 'HLA-A*02:208', 'HLA-A*02:209', 'HLA-A*02:21', 'HLA-A*02:210', 'HLA-A*02:211', 'HLA-A*02:212', 'HLA-A*02:213',
         'HLA-A*02:214', 'HLA-A*02:215', 'HLA-A*02:216', 'HLA-A*02:217', 'HLA-A*02:218', 'HLA-A*02:219', 'HLA-A*02:22', 'HLA-A*02:220',
         'HLA-A*02:221', 'HLA-A*02:224', 'HLA-A*02:228', 'HLA-A*02:229', 'HLA-A*02:230', 'HLA-A*02:231', 'HLA-A*02:232', 'HLA-A*02:233',
         'HLA-A*02:234', 'HLA-A*02:235', 'HLA-A*02:236', 'HLA-A*02:237', 'HLA-A*02:238', 'HLA-A*02:239', 'HLA-A*02:24', 'HLA-A*02:240',
         'HLA-A*02:241', 'HLA-A*02:242', 'HLA-A*02:243', 'HLA-A*02:244', 'HLA-A*02:245', 'HLA-A*02:246', 'HLA-A*02:247', 'HLA-A*02:248',
         'HLA-A*02:249', 'HLA-A*02:25', 'HLA-A*02:251', 'HLA-A*02:252', 'HLA-A*02:253', 'HLA-A*02:254', 'HLA-A*02:255', 'HLA-A*02:256',
         'HLA-A*02:257', 'HLA-A*02:258', 'HLA-A*02:259', 'HLA-A*02:26', 'HLA-A*02:260', 'HLA-A*02:261', 'HLA-A*02:262', 'HLA-A*02:263',
         'HLA-A*02:264', 'HLA-A*02:265', 'HLA-A*02:266', 'HLA-A*02:27', 'HLA-A*02:28', 'HLA-A*02:29', 'HLA-A*02:30', 'HLA-A*02:31', 'HLA-A*02:33',
         'HLA-A*02:34', 'HLA-A*02:35', 'HLA-A*02:36', 'HLA-A*02:37', 'HLA-A*02:38', 'HLA-A*02:39', 'HLA-A*02:40', 'HLA-A*02:41', 'HLA-A*02:42',
         'HLA-A*02:44', 'HLA-A*02:45', 'HLA-A*02:46', 'HLA-A*02:47', 'HLA-A*02:48', 'HLA-A*02:49', 'HLA-A*02:50', 'HLA-A*02:51', 'HLA-A*02:52',
         'HLA-A*02:54', 'HLA-A*02:55', 'HLA-A*02:56', 'HLA-A*02:57', 'HLA-A*02:58', 'HLA-A*02:59', 'HLA-A*02:60', 'HLA-A*02:61', 'HLA-A*02:62',
         'HLA-A*02:63', 'HLA-A*02:64', 'HLA-A*02:65', 'HLA-A*02:66', 'HLA-A*02:67', 'HLA-A*02:68', 'HLA-A*02:69', 'HLA-A*02:70', 'HLA-A*02:71',
         'HLA-A*02:72', 'HLA-A*02:73', 'HLA-A*02:74', 'HLA-A*02:75', 'HLA-A*02:76', 'HLA-A*02:77', 'HLA-A*02:78', 'HLA-A*02:79', 'HLA-A*02:80',
         'HLA-A*02:81', 'HLA-A*02:84', 'HLA-A*02:85', 'HLA-A*02:86', 'HLA-A*02:87', 'HLA-A*02:89', 'HLA-A*02:90', 'HLA-A*02:91', 'HLA-A*02:92',
         'HLA-A*02:93', 'HLA-A*02:95', 'HLA-A*02:96', 'HLA-A*02:97', 'HLA-A*02:99', 'HLA-A*03:01', 'HLA-A*03:02', 'HLA-A*03:04', 'HLA-A*03:05',
         'HLA-A*03:06', 'HLA-A*03:07', 'HLA-A*03:08', 'HLA-A*03:09', 'HLA-A*03:10', 'HLA-A*03:12', 'HLA-A*03:13', 'HLA-A*03:14', 'HLA-A*03:15',
         'HLA-A*03:16', 'HLA-A*03:17', 'HLA-A*03:18', 'HLA-A*03:19', 'HLA-A*03:20', 'HLA-A*03:22', 'HLA-A*03:23', 'HLA-A*03:24', 'HLA-A*03:25',
         'HLA-A*03:26', 'HLA-A*03:27', 'HLA-A*03:28', 'HLA-A*03:29', 'HLA-A*03:30', 'HLA-A*03:31', 'HLA-A*03:32', 'HLA-A*03:33', 'HLA-A*03:34',
         'HLA-A*03:35', 'HLA-A*03:37', 'HLA-A*03:38', 'HLA-A*03:39', 'HLA-A*03:40', 'HLA-A*03:41', 'HLA-A*03:42', 'HLA-A*03:43', 'HLA-A*03:44',
         'HLA-A*03:45', 'HLA-A*03:46', 'HLA-A*03:47', 'HLA-A*03:48', 'HLA-A*03:49', 'HLA-A*03:50', 'HLA-A*03:51', 'HLA-A*03:52', 'HLA-A*03:53',
         'HLA-A*03:54', 'HLA-A*03:55', 'HLA-A*03:56', 'HLA-A*03:57', 'HLA-A*03:58', 'HLA-A*03:59', 'HLA-A*03:60', 'HLA-A*03:61', 'HLA-A*03:62',
         'HLA-A*03:63', 'HLA-A*03:64', 'HLA-A*03:65', 'HLA-A*03:66', 'HLA-A*03:67', 'HLA-A*03:70', 'HLA-A*03:71', 'HLA-A*03:72', 'HLA-A*03:73',
         'HLA-A*03:74', 'HLA-A*03:75', 'HLA-A*03:76', 'HLA-A*03:77', 'HLA-A*03:78', 'HLA-A*03:79', 'HLA-A*03:80', 'HLA-A*03:81', 'HLA-A*03:82',
         'HLA-A*11:01', 'HLA-A*11:02', 'HLA-A*11:03', 'HLA-A*11:04', 'HLA-A*11:05', 'HLA-A*11:06', 'HLA-A*11:07', 'HLA-A*11:08', 'HLA-A*11:09',
         'HLA-A*11:10', 'HLA-A*11:11', 'HLA-A*11:12', 'HLA-A*11:13', 'HLA-A*11:14', 'HLA-A*11:15', 'HLA-A*11:16', 'HLA-A*11:17', 'HLA-A*11:18',
         'HLA-A*11:19', 'HLA-A*11:20', 'HLA-A*11:22', 'HLA-A*11:23', 'HLA-A*11:24', 'HLA-A*11:25', 'HLA-A*11:26', 'HLA-A*11:27', 'HLA-A*11:29',
         'HLA-A*11:30', 'HLA-A*11:31', 'HLA-A*11:32', 'HLA-A*11:33', 'HLA-A*11:34', 'HLA-A*11:35', 'HLA-A*11:36', 'HLA-A*11:37', 'HLA-A*11:38',
         'HLA-A*11:39', 'HLA-A*11:40', 'HLA-A*11:41', 'HLA-A*11:42', 'HLA-A*11:43', 'HLA-A*11:44', 'HLA-A*11:45', 'HLA-A*11:46', 'HLA-A*11:47',
         'HLA-A*11:48', 'HLA-A*11:49', 'HLA-A*11:51', 'HLA-A*11:53', 'HLA-A*11:54', 'HLA-A*11:55', 'HLA-A*11:56', 'HLA-A*11:57', 'HLA-A*11:58',
         'HLA-A*11:59', 'HLA-A*11:60', 'HLA-A*11:61', 'HLA-A*11:62', 'HLA-A*11:63', 'HLA-A*11:64', 'HLA-A*23:01', 'HLA-A*23:02', 'HLA-A*23:03',
         'HLA-A*23:04', 'HLA-A*23:05', 'HLA-A*23:06', 'HLA-A*23:09', 'HLA-A*23:10', 'HLA-A*23:12', 'HLA-A*23:13', 'HLA-A*23:14', 'HLA-A*23:15',
         'HLA-A*23:16', 'HLA-A*23:17', 'HLA-A*23:18', 'HLA-A*23:20', 'HLA-A*23:21', 'HLA-A*23:22', 'HLA-A*23:23', 'HLA-A*23:24', 'HLA-A*23:25',
         'HLA-A*23:26', 'HLA-A*24:02', 'HLA-A*24:03', 'HLA-A*24:04', 'HLA-A*24:05', 'HLA-A*24:06', 'HLA-A*24:07', 'HLA-A*24:08', 'HLA-A*24:10',
         'HLA-A*24:100', 'HLA-A*24:101', 'HLA-A*24:102', 'HLA-A*24:103', 'HLA-A*24:104', 'HLA-A*24:105', 'HLA-A*24:106', 'HLA-A*24:107',
         'HLA-A*24:108', 'HLA-A*24:109', 'HLA-A*24:110', 'HLA-A*24:111', 'HLA-A*24:112', 'HLA-A*24:113', 'HLA-A*24:114', 'HLA-A*24:115',
         'HLA-A*24:116', 'HLA-A*24:117', 'HLA-A*24:118', 'HLA-A*24:119', 'HLA-A*24:120', 'HLA-A*24:121', 'HLA-A*24:122', 'HLA-A*24:123',
         'HLA-A*24:124', 'HLA-A*24:125', 'HLA-A*24:126', 'HLA-A*24:127', 'HLA-A*24:128', 'HLA-A*24:129', 'HLA-A*24:13', 'HLA-A*24:130',
         'HLA-A*24:131', 'HLA-A*24:133', 'HLA-A*24:134', 'HLA-A*24:135', 'HLA-A*24:136', 'HLA-A*24:137', 'HLA-A*24:138', 'HLA-A*24:139',
         'HLA-A*24:14', 'HLA-A*24:140', 'HLA-A*24:141', 'HLA-A*24:142', 'HLA-A*24:143', 'HLA-A*24:144', 'HLA-A*24:15', 'HLA-A*24:17', 'HLA-A*24:18',
         'HLA-A*24:19', 'HLA-A*24:20', 'HLA-A*24:21', 'HLA-A*24:22', 'HLA-A*24:23', 'HLA-A*24:24', 'HLA-A*24:25', 'HLA-A*24:26', 'HLA-A*24:27',
         'HLA-A*24:28', 'HLA-A*24:29', 'HLA-A*24:30', 'HLA-A*24:31', 'HLA-A*24:32', 'HLA-A*24:33', 'HLA-A*24:34', 'HLA-A*24:35', 'HLA-A*24:37',
         'HLA-A*24:38', 'HLA-A*24:39', 'HLA-A*24:41', 'HLA-A*24:42', 'HLA-A*24:43', 'HLA-A*24:44', 'HLA-A*24:46', 'HLA-A*24:47', 'HLA-A*24:49',
         'HLA-A*24:50', 'HLA-A*24:51', 'HLA-A*24:52', 'HLA-A*24:53', 'HLA-A*24:54', 'HLA-A*24:55', 'HLA-A*24:56', 'HLA-A*24:57', 'HLA-A*24:58',
         'HLA-A*24:59', 'HLA-A*24:61', 'HLA-A*24:62', 'HLA-A*24:63', 'HLA-A*24:64', 'HLA-A*24:66', 'HLA-A*24:67', 'HLA-A*24:68', 'HLA-A*24:69',
         'HLA-A*24:70', 'HLA-A*24:71', 'HLA-A*24:72', 'HLA-A*24:73', 'HLA-A*24:74', 'HLA-A*24:75', 'HLA-A*24:76', 'HLA-A*24:77', 'HLA-A*24:78',
         'HLA-A*24:79', 'HLA-A*24:80', 'HLA-A*24:81', 'HLA-A*24:82', 'HLA-A*24:85', 'HLA-A*24:87', 'HLA-A*24:88', 'HLA-A*24:89', 'HLA-A*24:91',
         'HLA-A*24:92', 'HLA-A*24:93', 'HLA-A*24:94', 'HLA-A*24:95', 'HLA-A*24:96', 'HLA-A*24:97', 'HLA-A*24:98', 'HLA-A*24:99', 'HLA-A*25:01',
         'HLA-A*25:02', 'HLA-A*25:03', 'HLA-A*25:04', 'HLA-A*25:05', 'HLA-A*25:06', 'HLA-A*25:07', 'HLA-A*25:08', 'HLA-A*25:09', 'HLA-A*25:10',
         'HLA-A*25:11', 'HLA-A*25:13', 'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*26:04', 'HLA-A*26:05', 'HLA-A*26:06', 'HLA-A*26:07',
         'HLA-A*26:08', 'HLA-A*26:09', 'HLA-A*26:10', 'HLA-A*26:12', 'HLA-A*26:13', 'HLA-A*26:14', 'HLA-A*26:15', 'HLA-A*26:16', 'HLA-A*26:17',
         'HLA-A*26:18', 'HLA-A*26:19', 'HLA-A*26:20', 'HLA-A*26:21', 'HLA-A*26:22', 'HLA-A*26:23', 'HLA-A*26:24', 'HLA-A*26:26', 'HLA-A*26:27',
         'HLA-A*26:28', 'HLA-A*26:29', 'HLA-A*26:30', 'HLA-A*26:31', 'HLA-A*26:32', 'HLA-A*26:33', 'HLA-A*26:34', 'HLA-A*26:35', 'HLA-A*26:36',
         'HLA-A*26:37', 'HLA-A*26:38', 'HLA-A*26:39', 'HLA-A*26:40', 'HLA-A*26:41', 'HLA-A*26:42', 'HLA-A*26:43', 'HLA-A*26:45', 'HLA-A*26:46',
         'HLA-A*26:47', 'HLA-A*26:48', 'HLA-A*26:49', 'HLA-A*26:50', 'HLA-A*29:01', 'HLA-A*29:02', 'HLA-A*29:03', 'HLA-A*29:04', 'HLA-A*29:05',
         'HLA-A*29:06', 'HLA-A*29:07', 'HLA-A*29:09', 'HLA-A*29:10', 'HLA-A*29:11', 'HLA-A*29:12', 'HLA-A*29:13', 'HLA-A*29:14', 'HLA-A*29:15',
         'HLA-A*29:16', 'HLA-A*29:17', 'HLA-A*29:18', 'HLA-A*29:19', 'HLA-A*29:20', 'HLA-A*29:21', 'HLA-A*29:22', 'HLA-A*30:01', 'HLA-A*30:02',
         'HLA-A*30:03', 'HLA-A*30:04', 'HLA-A*30:06', 'HLA-A*30:07', 'HLA-A*30:08', 'HLA-A*30:09', 'HLA-A*30:10', 'HLA-A*30:11', 'HLA-A*30:12',
         'HLA-A*30:13', 'HLA-A*30:15', 'HLA-A*30:16', 'HLA-A*30:17', 'HLA-A*30:18', 'HLA-A*30:19', 'HLA-A*30:20', 'HLA-A*30:22', 'HLA-A*30:23',
         'HLA-A*30:24', 'HLA-A*30:25', 'HLA-A*30:26', 'HLA-A*30:28', 'HLA-A*30:29', 'HLA-A*30:30', 'HLA-A*30:31', 'HLA-A*30:32', 'HLA-A*30:33',
         'HLA-A*30:34', 'HLA-A*30:35', 'HLA-A*30:36', 'HLA-A*30:37', 'HLA-A*30:38', 'HLA-A*30:39', 'HLA-A*30:40', 'HLA-A*30:41', 'HLA-A*31:01',
         'HLA-A*31:02', 'HLA-A*31:03', 'HLA-A*31:04', 'HLA-A*31:05', 'HLA-A*31:06', 'HLA-A*31:07', 'HLA-A*31:08', 'HLA-A*31:09', 'HLA-A*31:10',
         'HLA-A*31:11', 'HLA-A*31:12', 'HLA-A*31:13', 'HLA-A*31:15', 'HLA-A*31:16', 'HLA-A*31:17', 'HLA-A*31:18', 'HLA-A*31:19', 'HLA-A*31:20',
         'HLA-A*31:21', 'HLA-A*31:22', 'HLA-A*31:23', 'HLA-A*31:24', 'HLA-A*31:25', 'HLA-A*31:26', 'HLA-A*31:27', 'HLA-A*31:28', 'HLA-A*31:29',
         'HLA-A*31:30', 'HLA-A*31:31', 'HLA-A*31:32', 'HLA-A*31:33', 'HLA-A*31:34', 'HLA-A*31:35', 'HLA-A*31:36', 'HLA-A*31:37', 'HLA-A*32:01',
         'HLA-A*32:02', 'HLA-A*32:03', 'HLA-A*32:04', 'HLA-A*32:05', 'HLA-A*32:06', 'HLA-A*32:07', 'HLA-A*32:08', 'HLA-A*32:09', 'HLA-A*32:10',
         'HLA-A*32:12', 'HLA-A*32:13', 'HLA-A*32:14', 'HLA-A*32:15', 'HLA-A*32:16', 'HLA-A*32:17', 'HLA-A*32:18', 'HLA-A*32:20', 'HLA-A*32:21',
         'HLA-A*32:22', 'HLA-A*32:23', 'HLA-A*32:24', 'HLA-A*32:25', 'HLA-A*33:01', 'HLA-A*33:03', 'HLA-A*33:04', 'HLA-A*33:05', 'HLA-A*33:06',
         'HLA-A*33:07', 'HLA-A*33:08', 'HLA-A*33:09', 'HLA-A*33:10', 'HLA-A*33:11', 'HLA-A*33:12', 'HLA-A*33:13', 'HLA-A*33:14', 'HLA-A*33:15',
         'HLA-A*33:16', 'HLA-A*33:17', 'HLA-A*33:18', 'HLA-A*33:19', 'HLA-A*33:20', 'HLA-A*33:21', 'HLA-A*33:22', 'HLA-A*33:23', 'HLA-A*33:24',
         'HLA-A*33:25', 'HLA-A*33:26', 'HLA-A*33:27', 'HLA-A*33:28', 'HLA-A*33:29', 'HLA-A*33:30', 'HLA-A*33:31', 'HLA-A*34:01', 'HLA-A*34:02',
         'HLA-A*34:03', 'HLA-A*34:04', 'HLA-A*34:05', 'HLA-A*34:06', 'HLA-A*34:07', 'HLA-A*34:08', 'HLA-A*36:01', 'HLA-A*36:02', 'HLA-A*36:03',
         'HLA-A*36:04', 'HLA-A*36:05', 'HLA-A*43:01', 'HLA-A*66:01', 'HLA-A*66:02', 'HLA-A*66:03', 'HLA-A*66:04', 'HLA-A*66:05', 'HLA-A*66:06',
         'HLA-A*66:07', 'HLA-A*66:08', 'HLA-A*66:09', 'HLA-A*66:10', 'HLA-A*66:11', 'HLA-A*66:12', 'HLA-A*66:13', 'HLA-A*66:14', 'HLA-A*66:15',
         'HLA-A*68:01', 'HLA-A*68:02', 'HLA-A*68:03', 'HLA-A*68:04', 'HLA-A*68:05', 'HLA-A*68:06', 'HLA-A*68:07', 'HLA-A*68:08', 'HLA-A*68:09',
         'HLA-A*68:10', 'HLA-A*68:12', 'HLA-A*68:13', 'HLA-A*68:14', 'HLA-A*68:15', 'HLA-A*68:16', 'HLA-A*68:17', 'HLA-A*68:19', 'HLA-A*68:20',
         'HLA-A*68:21', 'HLA-A*68:22', 'HLA-A*68:23', 'HLA-A*68:24', 'HLA-A*68:25', 'HLA-A*68:26', 'HLA-A*68:27', 'HLA-A*68:28', 'HLA-A*68:29',
         'HLA-A*68:30', 'HLA-A*68:31', 'HLA-A*68:32', 'HLA-A*68:33', 'HLA-A*68:34', 'HLA-A*68:35', 'HLA-A*68:36', 'HLA-A*68:37', 'HLA-A*68:38',
         'HLA-A*68:39', 'HLA-A*68:40', 'HLA-A*68:41', 'HLA-A*68:42', 'HLA-A*68:43', 'HLA-A*68:44', 'HLA-A*68:45', 'HLA-A*68:46', 'HLA-A*68:47',
         'HLA-A*68:48', 'HLA-A*68:50', 'HLA-A*68:51', 'HLA-A*68:52', 'HLA-A*68:53', 'HLA-A*68:54', 'HLA-A*69:01', 'HLA-A*74:01', 'HLA-A*74:02',
         'HLA-A*74:03', 'HLA-A*74:04', 'HLA-A*74:05', 'HLA-A*74:06', 'HLA-A*74:07', 'HLA-A*74:08', 'HLA-A*74:09', 'HLA-A*74:10', 'HLA-A*74:11',
         'HLA-A*74:13', 'HLA-A*80:01', 'HLA-A*80:02', 'HLA-B*07:02', 'HLA-B*07:03', 'HLA-B*07:04', 'HLA-B*07:05', 'HLA-B*07:06', 'HLA-B*07:07',
         'HLA-B*07:08', 'HLA-B*07:09', 'HLA-B*07:10', 'HLA-B*07:100', 'HLA-B*07:101', 'HLA-B*07:102', 'HLA-B*07:103', 'HLA-B*07:104',
         'HLA-B*07:105', 'HLA-B*07:106', 'HLA-B*07:107', 'HLA-B*07:108', 'HLA-B*07:109', 'HLA-B*07:11', 'HLA-B*07:110', 'HLA-B*07:112',
         'HLA-B*07:113', 'HLA-B*07:114', 'HLA-B*07:115', 'HLA-B*07:12', 'HLA-B*07:13', 'HLA-B*07:14', 'HLA-B*07:15', 'HLA-B*07:16', 'HLA-B*07:17',
         'HLA-B*07:18', 'HLA-B*07:19', 'HLA-B*07:20', 'HLA-B*07:21', 'HLA-B*07:22', 'HLA-B*07:23', 'HLA-B*07:24', 'HLA-B*07:25', 'HLA-B*07:26',
         'HLA-B*07:27', 'HLA-B*07:28', 'HLA-B*07:29', 'HLA-B*07:30', 'HLA-B*07:31', 'HLA-B*07:32', 'HLA-B*07:33', 'HLA-B*07:34', 'HLA-B*07:35',
         'HLA-B*07:36', 'HLA-B*07:37', 'HLA-B*07:38', 'HLA-B*07:39', 'HLA-B*07:40', 'HLA-B*07:41', 'HLA-B*07:42', 'HLA-B*07:43', 'HLA-B*07:44',
         'HLA-B*07:45', 'HLA-B*07:46', 'HLA-B*07:47', 'HLA-B*07:48', 'HLA-B*07:50', 'HLA-B*07:51', 'HLA-B*07:52', 'HLA-B*07:53', 'HLA-B*07:54',
         'HLA-B*07:55', 'HLA-B*07:56', 'HLA-B*07:57', 'HLA-B*07:58', 'HLA-B*07:59', 'HLA-B*07:60', 'HLA-B*07:61', 'HLA-B*07:62', 'HLA-B*07:63',
         'HLA-B*07:64', 'HLA-B*07:65', 'HLA-B*07:66', 'HLA-B*07:68', 'HLA-B*07:69', 'HLA-B*07:70', 'HLA-B*07:71', 'HLA-B*07:72', 'HLA-B*07:73',
         'HLA-B*07:74', 'HLA-B*07:75', 'HLA-B*07:76', 'HLA-B*07:77', 'HLA-B*07:78', 'HLA-B*07:79', 'HLA-B*07:80', 'HLA-B*07:81', 'HLA-B*07:82',
         'HLA-B*07:83', 'HLA-B*07:84', 'HLA-B*07:85', 'HLA-B*07:86', 'HLA-B*07:87', 'HLA-B*07:88', 'HLA-B*07:89', 'HLA-B*07:90', 'HLA-B*07:91',
         'HLA-B*07:92', 'HLA-B*07:93', 'HLA-B*07:94', 'HLA-B*07:95', 'HLA-B*07:96', 'HLA-B*07:97', 'HLA-B*07:98', 'HLA-B*07:99', 'HLA-B*08:01',
         'HLA-B*08:02', 'HLA-B*08:03', 'HLA-B*08:04', 'HLA-B*08:05', 'HLA-B*08:07', 'HLA-B*08:09', 'HLA-B*08:10', 'HLA-B*08:11', 'HLA-B*08:12',
         'HLA-B*08:13', 'HLA-B*08:14', 'HLA-B*08:15', 'HLA-B*08:16', 'HLA-B*08:17', 'HLA-B*08:18', 'HLA-B*08:20', 'HLA-B*08:21', 'HLA-B*08:22',
         'HLA-B*08:23', 'HLA-B*08:24', 'HLA-B*08:25', 'HLA-B*08:26', 'HLA-B*08:27', 'HLA-B*08:28', 'HLA-B*08:29', 'HLA-B*08:31', 'HLA-B*08:32',
         'HLA-B*08:33', 'HLA-B*08:34', 'HLA-B*08:35', 'HLA-B*08:36', 'HLA-B*08:37', 'HLA-B*08:38', 'HLA-B*08:39', 'HLA-B*08:40', 'HLA-B*08:41',
         'HLA-B*08:42', 'HLA-B*08:43', 'HLA-B*08:44', 'HLA-B*08:45', 'HLA-B*08:46', 'HLA-B*08:47', 'HLA-B*08:48', 'HLA-B*08:49', 'HLA-B*08:50',
         'HLA-B*08:51', 'HLA-B*08:52', 'HLA-B*08:53', 'HLA-B*08:54', 'HLA-B*08:55', 'HLA-B*08:56', 'HLA-B*08:57', 'HLA-B*08:58', 'HLA-B*08:59',
         'HLA-B*08:60', 'HLA-B*08:61', 'HLA-B*08:62', 'HLA-B*13:01', 'HLA-B*13:02', 'HLA-B*13:03', 'HLA-B*13:04', 'HLA-B*13:06', 'HLA-B*13:09',
         'HLA-B*13:10', 'HLA-B*13:11', 'HLA-B*13:12', 'HLA-B*13:13', 'HLA-B*13:14', 'HLA-B*13:15', 'HLA-B*13:16', 'HLA-B*13:17', 'HLA-B*13:18',
         'HLA-B*13:19', 'HLA-B*13:20', 'HLA-B*13:21', 'HLA-B*13:22', 'HLA-B*13:23', 'HLA-B*13:25', 'HLA-B*13:26', 'HLA-B*13:27', 'HLA-B*13:28',
         'HLA-B*13:29', 'HLA-B*13:30', 'HLA-B*13:31', 'HLA-B*13:32', 'HLA-B*13:33', 'HLA-B*13:34', 'HLA-B*13:35', 'HLA-B*13:36', 'HLA-B*13:37',
         'HLA-B*13:38', 'HLA-B*13:39', 'HLA-B*14:01', 'HLA-B*14:02', 'HLA-B*14:03', 'HLA-B*14:04', 'HLA-B*14:05', 'HLA-B*14:06', 'HLA-B*14:08',
         'HLA-B*14:09', 'HLA-B*14:10', 'HLA-B*14:11', 'HLA-B*14:12', 'HLA-B*14:13', 'HLA-B*14:14', 'HLA-B*14:15', 'HLA-B*14:16', 'HLA-B*14:17',
         'HLA-B*14:18', 'HLA-B*15:01', 'HLA-B*15:02', 'HLA-B*15:03', 'HLA-B*15:04', 'HLA-B*15:05', 'HLA-B*15:06', 'HLA-B*15:07', 'HLA-B*15:08',
         'HLA-B*15:09', 'HLA-B*15:10', 'HLA-B*15:101', 'HLA-B*15:102', 'HLA-B*15:103', 'HLA-B*15:104', 'HLA-B*15:105', 'HLA-B*15:106',
         'HLA-B*15:107', 'HLA-B*15:108', 'HLA-B*15:109', 'HLA-B*15:11', 'HLA-B*15:110', 'HLA-B*15:112', 'HLA-B*15:113', 'HLA-B*15:114',
         'HLA-B*15:115', 'HLA-B*15:116', 'HLA-B*15:117', 'HLA-B*15:118', 'HLA-B*15:119', 'HLA-B*15:12', 'HLA-B*15:120', 'HLA-B*15:121',
         'HLA-B*15:122', 'HLA-B*15:123', 'HLA-B*15:124', 'HLA-B*15:125', 'HLA-B*15:126', 'HLA-B*15:127', 'HLA-B*15:128', 'HLA-B*15:129',
         'HLA-B*15:13', 'HLA-B*15:131', 'HLA-B*15:132', 'HLA-B*15:133', 'HLA-B*15:134', 'HLA-B*15:135', 'HLA-B*15:136', 'HLA-B*15:137',
         'HLA-B*15:138', 'HLA-B*15:139', 'HLA-B*15:14', 'HLA-B*15:140', 'HLA-B*15:141', 'HLA-B*15:142', 'HLA-B*15:143', 'HLA-B*15:144',
         'HLA-B*15:145', 'HLA-B*15:146', 'HLA-B*15:147', 'HLA-B*15:148', 'HLA-B*15:15', 'HLA-B*15:150', 'HLA-B*15:151', 'HLA-B*15:152',
         'HLA-B*15:153', 'HLA-B*15:154', 'HLA-B*15:155', 'HLA-B*15:156', 'HLA-B*15:157', 'HLA-B*15:158', 'HLA-B*15:159', 'HLA-B*15:16',
         'HLA-B*15:160', 'HLA-B*15:161', 'HLA-B*15:162', 'HLA-B*15:163', 'HLA-B*15:164', 'HLA-B*15:165', 'HLA-B*15:166', 'HLA-B*15:167',
         'HLA-B*15:168', 'HLA-B*15:169', 'HLA-B*15:17', 'HLA-B*15:170', 'HLA-B*15:171', 'HLA-B*15:172', 'HLA-B*15:173', 'HLA-B*15:174',
         'HLA-B*15:175', 'HLA-B*15:176', 'HLA-B*15:177', 'HLA-B*15:178', 'HLA-B*15:179', 'HLA-B*15:18', 'HLA-B*15:180', 'HLA-B*15:183',
         'HLA-B*15:184', 'HLA-B*15:185', 'HLA-B*15:186', 'HLA-B*15:187', 'HLA-B*15:188', 'HLA-B*15:189', 'HLA-B*15:19', 'HLA-B*15:191',
         'HLA-B*15:192', 'HLA-B*15:193', 'HLA-B*15:194', 'HLA-B*15:195', 'HLA-B*15:196', 'HLA-B*15:197', 'HLA-B*15:198', 'HLA-B*15:199',
         'HLA-B*15:20', 'HLA-B*15:200', 'HLA-B*15:201', 'HLA-B*15:202', 'HLA-B*15:21', 'HLA-B*15:23', 'HLA-B*15:24', 'HLA-B*15:25', 'HLA-B*15:27',
         'HLA-B*15:28', 'HLA-B*15:29', 'HLA-B*15:30', 'HLA-B*15:31', 'HLA-B*15:32', 'HLA-B*15:33', 'HLA-B*15:34', 'HLA-B*15:35', 'HLA-B*15:36',
         'HLA-B*15:37', 'HLA-B*15:38', 'HLA-B*15:39', 'HLA-B*15:40', 'HLA-B*15:42', 'HLA-B*15:43', 'HLA-B*15:44', 'HLA-B*15:45', 'HLA-B*15:46',
         'HLA-B*15:47', 'HLA-B*15:48', 'HLA-B*15:49', 'HLA-B*15:50', 'HLA-B*15:51', 'HLA-B*15:52', 'HLA-B*15:53', 'HLA-B*15:54', 'HLA-B*15:55',
         'HLA-B*15:56', 'HLA-B*15:57', 'HLA-B*15:58', 'HLA-B*15:60', 'HLA-B*15:61', 'HLA-B*15:62', 'HLA-B*15:63', 'HLA-B*15:64', 'HLA-B*15:65',
         'HLA-B*15:66', 'HLA-B*15:67', 'HLA-B*15:68', 'HLA-B*15:69', 'HLA-B*15:70', 'HLA-B*15:71', 'HLA-B*15:72', 'HLA-B*15:73', 'HLA-B*15:74',
         'HLA-B*15:75', 'HLA-B*15:76', 'HLA-B*15:77', 'HLA-B*15:78', 'HLA-B*15:80', 'HLA-B*15:81', 'HLA-B*15:82', 'HLA-B*15:83', 'HLA-B*15:84',
         'HLA-B*15:85', 'HLA-B*15:86', 'HLA-B*15:87', 'HLA-B*15:88', 'HLA-B*15:89', 'HLA-B*15:90', 'HLA-B*15:91', 'HLA-B*15:92', 'HLA-B*15:93',
         'HLA-B*15:95', 'HLA-B*15:96', 'HLA-B*15:97', 'HLA-B*15:98', 'HLA-B*15:99', 'HLA-B*18:01', 'HLA-B*18:02', 'HLA-B*18:03', 'HLA-B*18:04',
         'HLA-B*18:05', 'HLA-B*18:06', 'HLA-B*18:07', 'HLA-B*18:08', 'HLA-B*18:09', 'HLA-B*18:10', 'HLA-B*18:11', 'HLA-B*18:12', 'HLA-B*18:13',
         'HLA-B*18:14', 'HLA-B*18:15', 'HLA-B*18:18', 'HLA-B*18:19', 'HLA-B*18:20', 'HLA-B*18:21', 'HLA-B*18:22', 'HLA-B*18:24', 'HLA-B*18:25',
         'HLA-B*18:26', 'HLA-B*18:27', 'HLA-B*18:28', 'HLA-B*18:29', 'HLA-B*18:30', 'HLA-B*18:31', 'HLA-B*18:32', 'HLA-B*18:33', 'HLA-B*18:34',
         'HLA-B*18:35', 'HLA-B*18:36', 'HLA-B*18:37', 'HLA-B*18:38', 'HLA-B*18:39', 'HLA-B*18:40', 'HLA-B*18:41', 'HLA-B*18:42', 'HLA-B*18:43',
         'HLA-B*18:44', 'HLA-B*18:45', 'HLA-B*18:46', 'HLA-B*18:47', 'HLA-B*18:48', 'HLA-B*18:49', 'HLA-B*18:50', 'HLA-B*27:01', 'HLA-B*27:02',
         'HLA-B*27:03', 'HLA-B*27:04', 'HLA-B*27:05', 'HLA-B*27:06', 'HLA-B*27:07', 'HLA-B*27:08', 'HLA-B*27:09', 'HLA-B*27:10', 'HLA-B*27:11',
         'HLA-B*27:12', 'HLA-B*27:13', 'HLA-B*27:14', 'HLA-B*27:15', 'HLA-B*27:16', 'HLA-B*27:17', 'HLA-B*27:18', 'HLA-B*27:19', 'HLA-B*27:20',
         'HLA-B*27:21', 'HLA-B*27:23', 'HLA-B*27:24', 'HLA-B*27:25', 'HLA-B*27:26', 'HLA-B*27:27', 'HLA-B*27:28', 'HLA-B*27:29', 'HLA-B*27:30',
         'HLA-B*27:31', 'HLA-B*27:32', 'HLA-B*27:33', 'HLA-B*27:34', 'HLA-B*27:35', 'HLA-B*27:36', 'HLA-B*27:37', 'HLA-B*27:38', 'HLA-B*27:39',
         'HLA-B*27:40', 'HLA-B*27:41', 'HLA-B*27:42', 'HLA-B*27:43', 'HLA-B*27:44', 'HLA-B*27:45', 'HLA-B*27:46', 'HLA-B*27:47', 'HLA-B*27:48',
         'HLA-B*27:49', 'HLA-B*27:50', 'HLA-B*27:51', 'HLA-B*27:52', 'HLA-B*27:53', 'HLA-B*27:54', 'HLA-B*27:55', 'HLA-B*27:56', 'HLA-B*27:57',
         'HLA-B*27:58', 'HLA-B*27:60', 'HLA-B*27:61', 'HLA-B*27:62', 'HLA-B*27:63', 'HLA-B*27:67', 'HLA-B*27:68', 'HLA-B*27:69', 'HLA-B*35:01',
         'HLA-B*35:02', 'HLA-B*35:03', 'HLA-B*35:04', 'HLA-B*35:05', 'HLA-B*35:06', 'HLA-B*35:07', 'HLA-B*35:08', 'HLA-B*35:09', 'HLA-B*35:10',
         'HLA-B*35:100', 'HLA-B*35:101', 'HLA-B*35:102', 'HLA-B*35:103', 'HLA-B*35:104', 'HLA-B*35:105', 'HLA-B*35:106', 'HLA-B*35:107',
         'HLA-B*35:108', 'HLA-B*35:109', 'HLA-B*35:11', 'HLA-B*35:110', 'HLA-B*35:111', 'HLA-B*35:112', 'HLA-B*35:113', 'HLA-B*35:114',
         'HLA-B*35:115', 'HLA-B*35:116', 'HLA-B*35:117', 'HLA-B*35:118', 'HLA-B*35:119', 'HLA-B*35:12', 'HLA-B*35:120', 'HLA-B*35:121',
         'HLA-B*35:122', 'HLA-B*35:123', 'HLA-B*35:124', 'HLA-B*35:125', 'HLA-B*35:126', 'HLA-B*35:127', 'HLA-B*35:128', 'HLA-B*35:13',
         'HLA-B*35:131', 'HLA-B*35:132', 'HLA-B*35:133', 'HLA-B*35:135', 'HLA-B*35:136', 'HLA-B*35:137', 'HLA-B*35:138', 'HLA-B*35:139',
         'HLA-B*35:14', 'HLA-B*35:140', 'HLA-B*35:141', 'HLA-B*35:142', 'HLA-B*35:143', 'HLA-B*35:144', 'HLA-B*35:15', 'HLA-B*35:16', 'HLA-B*35:17',
         'HLA-B*35:18', 'HLA-B*35:19', 'HLA-B*35:20', 'HLA-B*35:21', 'HLA-B*35:22', 'HLA-B*35:23', 'HLA-B*35:24', 'HLA-B*35:25', 'HLA-B*35:26',
         'HLA-B*35:27', 'HLA-B*35:28', 'HLA-B*35:29', 'HLA-B*35:30', 'HLA-B*35:31', 'HLA-B*35:32', 'HLA-B*35:33', 'HLA-B*35:34', 'HLA-B*35:35',
         'HLA-B*35:36', 'HLA-B*35:37', 'HLA-B*35:38', 'HLA-B*35:39', 'HLA-B*35:41', 'HLA-B*35:42', 'HLA-B*35:43', 'HLA-B*35:44', 'HLA-B*35:45',
         'HLA-B*35:46', 'HLA-B*35:47', 'HLA-B*35:48', 'HLA-B*35:49', 'HLA-B*35:50', 'HLA-B*35:51', 'HLA-B*35:52', 'HLA-B*35:54', 'HLA-B*35:55',
         'HLA-B*35:56', 'HLA-B*35:57', 'HLA-B*35:58', 'HLA-B*35:59', 'HLA-B*35:60', 'HLA-B*35:61', 'HLA-B*35:62', 'HLA-B*35:63', 'HLA-B*35:64',
         'HLA-B*35:66', 'HLA-B*35:67', 'HLA-B*35:68', 'HLA-B*35:69', 'HLA-B*35:70', 'HLA-B*35:71', 'HLA-B*35:72', 'HLA-B*35:74', 'HLA-B*35:75',
         'HLA-B*35:76', 'HLA-B*35:77', 'HLA-B*35:78', 'HLA-B*35:79', 'HLA-B*35:80', 'HLA-B*35:81', 'HLA-B*35:82', 'HLA-B*35:83', 'HLA-B*35:84',
         'HLA-B*35:85', 'HLA-B*35:86', 'HLA-B*35:87', 'HLA-B*35:88', 'HLA-B*35:89', 'HLA-B*35:90', 'HLA-B*35:91', 'HLA-B*35:92', 'HLA-B*35:93',
         'HLA-B*35:94', 'HLA-B*35:95', 'HLA-B*35:96', 'HLA-B*35:97', 'HLA-B*35:98', 'HLA-B*35:99', 'HLA-B*37:01', 'HLA-B*37:02', 'HLA-B*37:04',
         'HLA-B*37:05', 'HLA-B*37:06', 'HLA-B*37:07', 'HLA-B*37:08', 'HLA-B*37:09', 'HLA-B*37:10', 'HLA-B*37:11', 'HLA-B*37:12', 'HLA-B*37:13',
         'HLA-B*37:14', 'HLA-B*37:15', 'HLA-B*37:17', 'HLA-B*37:18', 'HLA-B*37:19', 'HLA-B*37:20', 'HLA-B*37:21', 'HLA-B*37:22', 'HLA-B*37:23',
         'HLA-B*38:01', 'HLA-B*38:02', 'HLA-B*38:03', 'HLA-B*38:04', 'HLA-B*38:05', 'HLA-B*38:06', 'HLA-B*38:07', 'HLA-B*38:08', 'HLA-B*38:09',
         'HLA-B*38:10', 'HLA-B*38:11', 'HLA-B*38:12', 'HLA-B*38:13', 'HLA-B*38:14', 'HLA-B*38:15', 'HLA-B*38:16', 'HLA-B*38:17', 'HLA-B*38:18',
         'HLA-B*38:19', 'HLA-B*38:20', 'HLA-B*38:21', 'HLA-B*38:22', 'HLA-B*38:23', 'HLA-B*39:01', 'HLA-B*39:02', 'HLA-B*39:03', 'HLA-B*39:04',
         'HLA-B*39:05', 'HLA-B*39:06', 'HLA-B*39:07', 'HLA-B*39:08', 'HLA-B*39:09', 'HLA-B*39:10', 'HLA-B*39:11', 'HLA-B*39:12', 'HLA-B*39:13',
         'HLA-B*39:14', 'HLA-B*39:15', 'HLA-B*39:16', 'HLA-B*39:17', 'HLA-B*39:18', 'HLA-B*39:19', 'HLA-B*39:20', 'HLA-B*39:22', 'HLA-B*39:23',
         'HLA-B*39:24', 'HLA-B*39:26', 'HLA-B*39:27', 'HLA-B*39:28', 'HLA-B*39:29', 'HLA-B*39:30', 'HLA-B*39:31', 'HLA-B*39:32', 'HLA-B*39:33',
         'HLA-B*39:34', 'HLA-B*39:35', 'HLA-B*39:36', 'HLA-B*39:37', 'HLA-B*39:39', 'HLA-B*39:41', 'HLA-B*39:42', 'HLA-B*39:43', 'HLA-B*39:44',
         'HLA-B*39:45', 'HLA-B*39:46', 'HLA-B*39:47', 'HLA-B*39:48', 'HLA-B*39:49', 'HLA-B*39:50', 'HLA-B*39:51', 'HLA-B*39:52', 'HLA-B*39:53',
         'HLA-B*39:54', 'HLA-B*39:55', 'HLA-B*39:56', 'HLA-B*39:57', 'HLA-B*39:58', 'HLA-B*39:59', 'HLA-B*39:60', 'HLA-B*40:01', 'HLA-B*40:02',
         'HLA-B*40:03', 'HLA-B*40:04', 'HLA-B*40:05', 'HLA-B*40:06', 'HLA-B*40:07', 'HLA-B*40:08', 'HLA-B*40:09', 'HLA-B*40:10', 'HLA-B*40:100',
         'HLA-B*40:101', 'HLA-B*40:102', 'HLA-B*40:103', 'HLA-B*40:104', 'HLA-B*40:105', 'HLA-B*40:106', 'HLA-B*40:107', 'HLA-B*40:108',
         'HLA-B*40:109', 'HLA-B*40:11', 'HLA-B*40:110', 'HLA-B*40:111', 'HLA-B*40:112', 'HLA-B*40:113', 'HLA-B*40:114', 'HLA-B*40:115',
         'HLA-B*40:116', 'HLA-B*40:117', 'HLA-B*40:119', 'HLA-B*40:12', 'HLA-B*40:120', 'HLA-B*40:121', 'HLA-B*40:122', 'HLA-B*40:123',
         'HLA-B*40:124', 'HLA-B*40:125', 'HLA-B*40:126', 'HLA-B*40:127', 'HLA-B*40:128', 'HLA-B*40:129', 'HLA-B*40:13', 'HLA-B*40:130',
         'HLA-B*40:131', 'HLA-B*40:132', 'HLA-B*40:134', 'HLA-B*40:135', 'HLA-B*40:136', 'HLA-B*40:137', 'HLA-B*40:138', 'HLA-B*40:139',
         'HLA-B*40:14', 'HLA-B*40:140', 'HLA-B*40:141', 'HLA-B*40:143', 'HLA-B*40:145', 'HLA-B*40:146', 'HLA-B*40:147', 'HLA-B*40:15',
         'HLA-B*40:16', 'HLA-B*40:18', 'HLA-B*40:19', 'HLA-B*40:20', 'HLA-B*40:21', 'HLA-B*40:23', 'HLA-B*40:24', 'HLA-B*40:25', 'HLA-B*40:26',
         'HLA-B*40:27', 'HLA-B*40:28', 'HLA-B*40:29', 'HLA-B*40:30', 'HLA-B*40:31', 'HLA-B*40:32', 'HLA-B*40:33', 'HLA-B*40:34', 'HLA-B*40:35',
         'HLA-B*40:36', 'HLA-B*40:37', 'HLA-B*40:38', 'HLA-B*40:39', 'HLA-B*40:40', 'HLA-B*40:42', 'HLA-B*40:43', 'HLA-B*40:44', 'HLA-B*40:45',
         'HLA-B*40:46', 'HLA-B*40:47', 'HLA-B*40:48', 'HLA-B*40:49', 'HLA-B*40:50', 'HLA-B*40:51', 'HLA-B*40:52', 'HLA-B*40:53', 'HLA-B*40:54',
         'HLA-B*40:55', 'HLA-B*40:56', 'HLA-B*40:57', 'HLA-B*40:58', 'HLA-B*40:59', 'HLA-B*40:60', 'HLA-B*40:61', 'HLA-B*40:62', 'HLA-B*40:63',
         'HLA-B*40:64', 'HLA-B*40:65', 'HLA-B*40:66', 'HLA-B*40:67', 'HLA-B*40:68', 'HLA-B*40:69', 'HLA-B*40:70', 'HLA-B*40:71', 'HLA-B*40:72',
         'HLA-B*40:73', 'HLA-B*40:74', 'HLA-B*40:75', 'HLA-B*40:76', 'HLA-B*40:77', 'HLA-B*40:78', 'HLA-B*40:79', 'HLA-B*40:80', 'HLA-B*40:81',
         'HLA-B*40:82', 'HLA-B*40:83', 'HLA-B*40:84', 'HLA-B*40:85', 'HLA-B*40:86', 'HLA-B*40:87', 'HLA-B*40:88', 'HLA-B*40:89', 'HLA-B*40:90',
         'HLA-B*40:91', 'HLA-B*40:92', 'HLA-B*40:93', 'HLA-B*40:94', 'HLA-B*40:95', 'HLA-B*40:96', 'HLA-B*40:97', 'HLA-B*40:98', 'HLA-B*40:99',
         'HLA-B*41:01', 'HLA-B*41:02', 'HLA-B*41:03', 'HLA-B*41:04', 'HLA-B*41:05', 'HLA-B*41:06', 'HLA-B*41:07', 'HLA-B*41:08', 'HLA-B*41:09',
         'HLA-B*41:10', 'HLA-B*41:11', 'HLA-B*41:12', 'HLA-B*42:01', 'HLA-B*42:02', 'HLA-B*42:04', 'HLA-B*42:05', 'HLA-B*42:06', 'HLA-B*42:07',
         'HLA-B*42:08', 'HLA-B*42:09', 'HLA-B*42:10', 'HLA-B*42:11', 'HLA-B*42:12', 'HLA-B*42:13', 'HLA-B*42:14', 'HLA-B*44:02', 'HLA-B*44:03',
         'HLA-B*44:04', 'HLA-B*44:05', 'HLA-B*44:06', 'HLA-B*44:07', 'HLA-B*44:08', 'HLA-B*44:09', 'HLA-B*44:10', 'HLA-B*44:100', 'HLA-B*44:101',
         'HLA-B*44:102', 'HLA-B*44:103', 'HLA-B*44:104', 'HLA-B*44:105', 'HLA-B*44:106', 'HLA-B*44:107', 'HLA-B*44:109', 'HLA-B*44:11',
         'HLA-B*44:110', 'HLA-B*44:12', 'HLA-B*44:13', 'HLA-B*44:14', 'HLA-B*44:15', 'HLA-B*44:16', 'HLA-B*44:17', 'HLA-B*44:18', 'HLA-B*44:20',
         'HLA-B*44:21', 'HLA-B*44:22', 'HLA-B*44:24', 'HLA-B*44:25', 'HLA-B*44:26', 'HLA-B*44:27', 'HLA-B*44:28', 'HLA-B*44:29', 'HLA-B*44:30',
         'HLA-B*44:31', 'HLA-B*44:32', 'HLA-B*44:33', 'HLA-B*44:34', 'HLA-B*44:35', 'HLA-B*44:36', 'HLA-B*44:37', 'HLA-B*44:38', 'HLA-B*44:39',
         'HLA-B*44:40', 'HLA-B*44:41', 'HLA-B*44:42', 'HLA-B*44:43', 'HLA-B*44:44', 'HLA-B*44:45', 'HLA-B*44:46', 'HLA-B*44:47', 'HLA-B*44:48',
         'HLA-B*44:49', 'HLA-B*44:50', 'HLA-B*44:51', 'HLA-B*44:53', 'HLA-B*44:54', 'HLA-B*44:55', 'HLA-B*44:57', 'HLA-B*44:59', 'HLA-B*44:60',
         'HLA-B*44:62', 'HLA-B*44:63', 'HLA-B*44:64', 'HLA-B*44:65', 'HLA-B*44:66', 'HLA-B*44:67', 'HLA-B*44:68', 'HLA-B*44:69', 'HLA-B*44:70',
         'HLA-B*44:71', 'HLA-B*44:72', 'HLA-B*44:73', 'HLA-B*44:74', 'HLA-B*44:75', 'HLA-B*44:76', 'HLA-B*44:77', 'HLA-B*44:78', 'HLA-B*44:79',
         'HLA-B*44:80', 'HLA-B*44:81', 'HLA-B*44:82', 'HLA-B*44:83', 'HLA-B*44:84', 'HLA-B*44:85', 'HLA-B*44:86', 'HLA-B*44:87', 'HLA-B*44:88',
         'HLA-B*44:89', 'HLA-B*44:90', 'HLA-B*44:91', 'HLA-B*44:92', 'HLA-B*44:93', 'HLA-B*44:94', 'HLA-B*44:95', 'HLA-B*44:96', 'HLA-B*44:97',
         'HLA-B*44:98', 'HLA-B*44:99', 'HLA-B*45:01', 'HLA-B*45:02', 'HLA-B*45:03', 'HLA-B*45:04', 'HLA-B*45:05', 'HLA-B*45:06', 'HLA-B*45:07',
         'HLA-B*45:08', 'HLA-B*45:09', 'HLA-B*45:10', 'HLA-B*45:11', 'HLA-B*45:12', 'HLA-B*46:01', 'HLA-B*46:02', 'HLA-B*46:03', 'HLA-B*46:04',
         'HLA-B*46:05', 'HLA-B*46:06', 'HLA-B*46:08', 'HLA-B*46:09', 'HLA-B*46:10', 'HLA-B*46:11', 'HLA-B*46:12', 'HLA-B*46:13', 'HLA-B*46:14',
         'HLA-B*46:16', 'HLA-B*46:17', 'HLA-B*46:18', 'HLA-B*46:19', 'HLA-B*46:20', 'HLA-B*46:21', 'HLA-B*46:22', 'HLA-B*46:23', 'HLA-B*46:24',
         'HLA-B*47:01', 'HLA-B*47:02', 'HLA-B*47:03', 'HLA-B*47:04', 'HLA-B*47:05', 'HLA-B*47:06', 'HLA-B*47:07', 'HLA-B*48:01', 'HLA-B*48:02',
         'HLA-B*48:03', 'HLA-B*48:04', 'HLA-B*48:05', 'HLA-B*48:06', 'HLA-B*48:07', 'HLA-B*48:08', 'HLA-B*48:09', 'HLA-B*48:10', 'HLA-B*48:11',
         'HLA-B*48:12', 'HLA-B*48:13', 'HLA-B*48:14', 'HLA-B*48:15', 'HLA-B*48:16', 'HLA-B*48:17', 'HLA-B*48:18', 'HLA-B*48:19', 'HLA-B*48:20',
         'HLA-B*48:21', 'HLA-B*48:22', 'HLA-B*48:23', 'HLA-B*49:01', 'HLA-B*49:02', 'HLA-B*49:03', 'HLA-B*49:04', 'HLA-B*49:05', 'HLA-B*49:06',
         'HLA-B*49:07', 'HLA-B*49:08', 'HLA-B*49:09', 'HLA-B*49:10', 'HLA-B*50:01', 'HLA-B*50:02', 'HLA-B*50:04', 'HLA-B*50:05', 'HLA-B*50:06',
         'HLA-B*50:07', 'HLA-B*50:08', 'HLA-B*50:09', 'HLA-B*51:01', 'HLA-B*51:02', 'HLA-B*51:03', 'HLA-B*51:04', 'HLA-B*51:05', 'HLA-B*51:06',
         'HLA-B*51:07', 'HLA-B*51:08', 'HLA-B*51:09', 'HLA-B*51:12', 'HLA-B*51:13', 'HLA-B*51:14', 'HLA-B*51:15', 'HLA-B*51:16', 'HLA-B*51:17',
         'HLA-B*51:18', 'HLA-B*51:19', 'HLA-B*51:20', 'HLA-B*51:21', 'HLA-B*51:22', 'HLA-B*51:23', 'HLA-B*51:24', 'HLA-B*51:26', 'HLA-B*51:28',
         'HLA-B*51:29', 'HLA-B*51:30', 'HLA-B*51:31', 'HLA-B*51:32', 'HLA-B*51:33', 'HLA-B*51:34', 'HLA-B*51:35', 'HLA-B*51:36', 'HLA-B*51:37',
         'HLA-B*51:38', 'HLA-B*51:39', 'HLA-B*51:40', 'HLA-B*51:42', 'HLA-B*51:43', 'HLA-B*51:45', 'HLA-B*51:46', 'HLA-B*51:48', 'HLA-B*51:49',
         'HLA-B*51:50', 'HLA-B*51:51', 'HLA-B*51:52', 'HLA-B*51:53', 'HLA-B*51:54', 'HLA-B*51:55', 'HLA-B*51:56', 'HLA-B*51:57', 'HLA-B*51:58',
         'HLA-B*51:59', 'HLA-B*51:60', 'HLA-B*51:61', 'HLA-B*51:62', 'HLA-B*51:63', 'HLA-B*51:64', 'HLA-B*51:65', 'HLA-B*51:66', 'HLA-B*51:67',
         'HLA-B*51:68', 'HLA-B*51:69', 'HLA-B*51:70', 'HLA-B*51:71', 'HLA-B*51:72', 'HLA-B*51:73', 'HLA-B*51:74', 'HLA-B*51:75', 'HLA-B*51:76',
         'HLA-B*51:77', 'HLA-B*51:78', 'HLA-B*51:79', 'HLA-B*51:80', 'HLA-B*51:81', 'HLA-B*51:82', 'HLA-B*51:83', 'HLA-B*51:84', 'HLA-B*51:85',
         'HLA-B*51:86', 'HLA-B*51:87', 'HLA-B*51:88', 'HLA-B*51:89', 'HLA-B*51:90', 'HLA-B*51:91', 'HLA-B*51:92', 'HLA-B*51:93', 'HLA-B*51:94',
         'HLA-B*51:95', 'HLA-B*51:96', 'HLA-B*52:01', 'HLA-B*52:02', 'HLA-B*52:03', 'HLA-B*52:04', 'HLA-B*52:05', 'HLA-B*52:06', 'HLA-B*52:07',
         'HLA-B*52:08', 'HLA-B*52:09', 'HLA-B*52:10', 'HLA-B*52:11', 'HLA-B*52:12', 'HLA-B*52:13', 'HLA-B*52:14', 'HLA-B*52:15', 'HLA-B*52:16',
         'HLA-B*52:17', 'HLA-B*52:18', 'HLA-B*52:19', 'HLA-B*52:20', 'HLA-B*52:21', 'HLA-B*53:01', 'HLA-B*53:02', 'HLA-B*53:03', 'HLA-B*53:04',
         'HLA-B*53:05', 'HLA-B*53:06', 'HLA-B*53:07', 'HLA-B*53:08', 'HLA-B*53:09', 'HLA-B*53:10', 'HLA-B*53:11', 'HLA-B*53:12', 'HLA-B*53:13',
         'HLA-B*53:14', 'HLA-B*53:15', 'HLA-B*53:16', 'HLA-B*53:17', 'HLA-B*53:18', 'HLA-B*53:19', 'HLA-B*53:20', 'HLA-B*53:21', 'HLA-B*53:22',
         'HLA-B*53:23', 'HLA-B*54:01', 'HLA-B*54:02', 'HLA-B*54:03', 'HLA-B*54:04', 'HLA-B*54:06', 'HLA-B*54:07', 'HLA-B*54:09', 'HLA-B*54:10',
         'HLA-B*54:11', 'HLA-B*54:12', 'HLA-B*54:13', 'HLA-B*54:14', 'HLA-B*54:15', 'HLA-B*54:16', 'HLA-B*54:17', 'HLA-B*54:18', 'HLA-B*54:19',
         'HLA-B*54:20', 'HLA-B*54:21', 'HLA-B*54:22', 'HLA-B*54:23', 'HLA-B*55:01', 'HLA-B*55:02', 'HLA-B*55:03', 'HLA-B*55:04', 'HLA-B*55:05',
         'HLA-B*55:07', 'HLA-B*55:08', 'HLA-B*55:09', 'HLA-B*55:10', 'HLA-B*55:11', 'HLA-B*55:12', 'HLA-B*55:13', 'HLA-B*55:14', 'HLA-B*55:15',
         'HLA-B*55:16', 'HLA-B*55:17', 'HLA-B*55:18', 'HLA-B*55:19', 'HLA-B*55:20', 'HLA-B*55:21', 'HLA-B*55:22', 'HLA-B*55:23', 'HLA-B*55:24',
         'HLA-B*55:25', 'HLA-B*55:26', 'HLA-B*55:27', 'HLA-B*55:28', 'HLA-B*55:29', 'HLA-B*55:30', 'HLA-B*55:31', 'HLA-B*55:32', 'HLA-B*55:33',
         'HLA-B*55:34', 'HLA-B*55:35', 'HLA-B*55:36', 'HLA-B*55:37', 'HLA-B*55:38', 'HLA-B*55:39', 'HLA-B*55:40', 'HLA-B*55:41', 'HLA-B*55:42',
         'HLA-B*55:43', 'HLA-B*56:01', 'HLA-B*56:02', 'HLA-B*56:03', 'HLA-B*56:04', 'HLA-B*56:05', 'HLA-B*56:06', 'HLA-B*56:07', 'HLA-B*56:08',
         'HLA-B*56:09', 'HLA-B*56:10', 'HLA-B*56:11', 'HLA-B*56:12', 'HLA-B*56:13', 'HLA-B*56:14', 'HLA-B*56:15', 'HLA-B*56:16', 'HLA-B*56:17',
         'HLA-B*56:18', 'HLA-B*56:20', 'HLA-B*56:21', 'HLA-B*56:22', 'HLA-B*56:23', 'HLA-B*56:24', 'HLA-B*56:25', 'HLA-B*56:26', 'HLA-B*56:27',
         'HLA-B*56:29', 'HLA-B*57:01', 'HLA-B*57:02', 'HLA-B*57:03', 'HLA-B*57:04', 'HLA-B*57:05', 'HLA-B*57:06', 'HLA-B*57:07', 'HLA-B*57:08',
         'HLA-B*57:09', 'HLA-B*57:10', 'HLA-B*57:11', 'HLA-B*57:12', 'HLA-B*57:13', 'HLA-B*57:14', 'HLA-B*57:15', 'HLA-B*57:16', 'HLA-B*57:17',
         'HLA-B*57:18', 'HLA-B*57:19', 'HLA-B*57:20', 'HLA-B*57:21', 'HLA-B*57:22', 'HLA-B*57:23', 'HLA-B*57:24', 'HLA-B*57:25', 'HLA-B*57:26',
         'HLA-B*57:27', 'HLA-B*57:29', 'HLA-B*57:30', 'HLA-B*57:31', 'HLA-B*57:32', 'HLA-B*58:01', 'HLA-B*58:02', 'HLA-B*58:04', 'HLA-B*58:05',
         'HLA-B*58:06', 'HLA-B*58:07', 'HLA-B*58:08', 'HLA-B*58:09', 'HLA-B*58:11', 'HLA-B*58:12', 'HLA-B*58:13', 'HLA-B*58:14', 'HLA-B*58:15',
         'HLA-B*58:16', 'HLA-B*58:18', 'HLA-B*58:19', 'HLA-B*58:20', 'HLA-B*58:21', 'HLA-B*58:22', 'HLA-B*58:23', 'HLA-B*58:24', 'HLA-B*58:25',
         'HLA-B*58:26', 'HLA-B*58:27', 'HLA-B*58:28', 'HLA-B*58:29', 'HLA-B*58:30', 'HLA-B*59:01', 'HLA-B*59:02', 'HLA-B*59:03', 'HLA-B*59:04',
         'HLA-B*59:05', 'HLA-B*67:01', 'HLA-B*67:02', 'HLA-B*73:01', 'HLA-B*73:02', 'HLA-B*78:01', 'HLA-B*78:02', 'HLA-B*78:03', 'HLA-B*78:04',
         'HLA-B*78:05', 'HLA-B*78:06', 'HLA-B*78:07', 'HLA-B*81:01', 'HLA-B*81:02', 'HLA-B*81:03', 'HLA-B*81:05', 'HLA-B*82:01', 'HLA-B*82:02',
         'HLA-B*82:03', 'HLA-B*83:01', 'HLA-C*01:02', 'HLA-C*01:03', 'HLA-C*01:04', 'HLA-C*01:05', 'HLA-C*01:06', 'HLA-C*01:07', 'HLA-C*01:08',
         'HLA-C*01:09', 'HLA-C*01:10', 'HLA-C*01:11', 'HLA-C*01:12', 'HLA-C*01:13', 'HLA-C*01:14', 'HLA-C*01:15', 'HLA-C*01:16', 'HLA-C*01:17',
         'HLA-C*01:18', 'HLA-C*01:19', 'HLA-C*01:20', 'HLA-C*01:21', 'HLA-C*01:22', 'HLA-C*01:23', 'HLA-C*01:24', 'HLA-C*01:25', 'HLA-C*01:26',
         'HLA-C*01:27', 'HLA-C*01:28', 'HLA-C*01:29', 'HLA-C*01:30', 'HLA-C*01:31', 'HLA-C*01:32', 'HLA-C*01:33', 'HLA-C*01:34', 'HLA-C*01:35',
         'HLA-C*01:36', 'HLA-C*01:38', 'HLA-C*01:39', 'HLA-C*01:40', 'HLA-C*02:02', 'HLA-C*02:03', 'HLA-C*02:04', 'HLA-C*02:05', 'HLA-C*02:06',
         'HLA-C*02:07', 'HLA-C*02:08', 'HLA-C*02:09', 'HLA-C*02:10', 'HLA-C*02:11', 'HLA-C*02:12', 'HLA-C*02:13', 'HLA-C*02:14', 'HLA-C*02:15',
         'HLA-C*02:16', 'HLA-C*02:17', 'HLA-C*02:18', 'HLA-C*02:19', 'HLA-C*02:20', 'HLA-C*02:21', 'HLA-C*02:22', 'HLA-C*02:23', 'HLA-C*02:24',
         'HLA-C*02:26', 'HLA-C*02:27', 'HLA-C*02:28', 'HLA-C*02:29', 'HLA-C*02:30', 'HLA-C*02:31', 'HLA-C*02:32', 'HLA-C*02:33', 'HLA-C*02:34',
         'HLA-C*02:35', 'HLA-C*02:36', 'HLA-C*02:37', 'HLA-C*02:39', 'HLA-C*02:40', 'HLA-C*03:01', 'HLA-C*03:02', 'HLA-C*03:03', 'HLA-C*03:04',
         'HLA-C*03:05', 'HLA-C*03:06', 'HLA-C*03:07', 'HLA-C*03:08', 'HLA-C*03:09', 'HLA-C*03:10', 'HLA-C*03:11', 'HLA-C*03:12', 'HLA-C*03:13',
         'HLA-C*03:14', 'HLA-C*03:15', 'HLA-C*03:16', 'HLA-C*03:17', 'HLA-C*03:18', 'HLA-C*03:19', 'HLA-C*03:21', 'HLA-C*03:23', 'HLA-C*03:24',
         'HLA-C*03:25', 'HLA-C*03:26', 'HLA-C*03:27', 'HLA-C*03:28', 'HLA-C*03:29', 'HLA-C*03:30', 'HLA-C*03:31', 'HLA-C*03:32', 'HLA-C*03:33',
         'HLA-C*03:34', 'HLA-C*03:35', 'HLA-C*03:36', 'HLA-C*03:37', 'HLA-C*03:38', 'HLA-C*03:39', 'HLA-C*03:40', 'HLA-C*03:41', 'HLA-C*03:42',
         'HLA-C*03:43', 'HLA-C*03:44', 'HLA-C*03:45', 'HLA-C*03:46', 'HLA-C*03:47', 'HLA-C*03:48', 'HLA-C*03:49', 'HLA-C*03:50', 'HLA-C*03:51',
         'HLA-C*03:52', 'HLA-C*03:53', 'HLA-C*03:54', 'HLA-C*03:55', 'HLA-C*03:56', 'HLA-C*03:57', 'HLA-C*03:58', 'HLA-C*03:59', 'HLA-C*03:60',
         'HLA-C*03:61', 'HLA-C*03:62', 'HLA-C*03:63', 'HLA-C*03:64', 'HLA-C*03:65', 'HLA-C*03:66', 'HLA-C*03:67', 'HLA-C*03:68', 'HLA-C*03:69',
         'HLA-C*03:70', 'HLA-C*03:71', 'HLA-C*03:72', 'HLA-C*03:73', 'HLA-C*03:74', 'HLA-C*03:75', 'HLA-C*03:76', 'HLA-C*03:77', 'HLA-C*03:78',
         'HLA-C*03:79', 'HLA-C*03:80', 'HLA-C*03:81', 'HLA-C*03:82', 'HLA-C*03:83', 'HLA-C*03:84', 'HLA-C*03:85', 'HLA-C*03:86', 'HLA-C*03:87',
         'HLA-C*03:88', 'HLA-C*03:89', 'HLA-C*03:90', 'HLA-C*03:91', 'HLA-C*03:92', 'HLA-C*03:93', 'HLA-C*03:94', 'HLA-C*04:01', 'HLA-C*04:03',
         'HLA-C*04:04', 'HLA-C*04:05', 'HLA-C*04:06', 'HLA-C*04:07', 'HLA-C*04:08', 'HLA-C*04:10', 'HLA-C*04:11', 'HLA-C*04:12', 'HLA-C*04:13',
         'HLA-C*04:14', 'HLA-C*04:15', 'HLA-C*04:16', 'HLA-C*04:17', 'HLA-C*04:18', 'HLA-C*04:19', 'HLA-C*04:20', 'HLA-C*04:23', 'HLA-C*04:24',
         'HLA-C*04:25', 'HLA-C*04:26', 'HLA-C*04:27', 'HLA-C*04:28', 'HLA-C*04:29', 'HLA-C*04:30', 'HLA-C*04:31', 'HLA-C*04:32', 'HLA-C*04:33',
         'HLA-C*04:34', 'HLA-C*04:35', 'HLA-C*04:36', 'HLA-C*04:37', 'HLA-C*04:38', 'HLA-C*04:39', 'HLA-C*04:40', 'HLA-C*04:41', 'HLA-C*04:42',
         'HLA-C*04:43', 'HLA-C*04:44', 'HLA-C*04:45', 'HLA-C*04:46', 'HLA-C*04:47', 'HLA-C*04:48', 'HLA-C*04:49', 'HLA-C*04:50', 'HLA-C*04:51',
         'HLA-C*04:52', 'HLA-C*04:53', 'HLA-C*04:54', 'HLA-C*04:55', 'HLA-C*04:56', 'HLA-C*04:57', 'HLA-C*04:58', 'HLA-C*04:60', 'HLA-C*04:61',
         'HLA-C*04:62', 'HLA-C*04:63', 'HLA-C*04:64', 'HLA-C*04:65', 'HLA-C*04:66', 'HLA-C*04:67', 'HLA-C*04:68', 'HLA-C*04:69', 'HLA-C*04:70',
         'HLA-C*05:01', 'HLA-C*05:03', 'HLA-C*05:04', 'HLA-C*05:05', 'HLA-C*05:06', 'HLA-C*05:08', 'HLA-C*05:09', 'HLA-C*05:10', 'HLA-C*05:11',
         'HLA-C*05:12', 'HLA-C*05:13', 'HLA-C*05:14', 'HLA-C*05:15', 'HLA-C*05:16', 'HLA-C*05:17', 'HLA-C*05:18', 'HLA-C*05:19', 'HLA-C*05:20',
         'HLA-C*05:21', 'HLA-C*05:22', 'HLA-C*05:23', 'HLA-C*05:24', 'HLA-C*05:25', 'HLA-C*05:26', 'HLA-C*05:27', 'HLA-C*05:28', 'HLA-C*05:29',
         'HLA-C*05:30', 'HLA-C*05:31', 'HLA-C*05:32', 'HLA-C*05:33', 'HLA-C*05:34', 'HLA-C*05:35', 'HLA-C*05:36', 'HLA-C*05:37', 'HLA-C*05:38',
         'HLA-C*05:39', 'HLA-C*05:40', 'HLA-C*05:41', 'HLA-C*05:42', 'HLA-C*05:43', 'HLA-C*05:44', 'HLA-C*05:45', 'HLA-C*06:02', 'HLA-C*06:03',
         'HLA-C*06:04', 'HLA-C*06:05', 'HLA-C*06:06', 'HLA-C*06:07', 'HLA-C*06:08', 'HLA-C*06:09', 'HLA-C*06:10', 'HLA-C*06:11', 'HLA-C*06:12',
         'HLA-C*06:13', 'HLA-C*06:14', 'HLA-C*06:15', 'HLA-C*06:17', 'HLA-C*06:18', 'HLA-C*06:19', 'HLA-C*06:20', 'HLA-C*06:21', 'HLA-C*06:22',
         'HLA-C*06:23', 'HLA-C*06:24', 'HLA-C*06:25', 'HLA-C*06:26', 'HLA-C*06:27', 'HLA-C*06:28', 'HLA-C*06:29', 'HLA-C*06:30', 'HLA-C*06:31',
         'HLA-C*06:32', 'HLA-C*06:33', 'HLA-C*06:34', 'HLA-C*06:35', 'HLA-C*06:36', 'HLA-C*06:37', 'HLA-C*06:38', 'HLA-C*06:39', 'HLA-C*06:40',
         'HLA-C*06:41', 'HLA-C*06:42', 'HLA-C*06:43', 'HLA-C*06:44', 'HLA-C*06:45', 'HLA-C*07:01', 'HLA-C*07:02', 'HLA-C*07:03', 'HLA-C*07:04',
         'HLA-C*07:05', 'HLA-C*07:06', 'HLA-C*07:07', 'HLA-C*07:08', 'HLA-C*07:09', 'HLA-C*07:10', 'HLA-C*07:100', 'HLA-C*07:101', 'HLA-C*07:102',
         'HLA-C*07:103', 'HLA-C*07:105', 'HLA-C*07:106', 'HLA-C*07:107', 'HLA-C*07:108', 'HLA-C*07:109', 'HLA-C*07:11', 'HLA-C*07:110',
         'HLA-C*07:111', 'HLA-C*07:112', 'HLA-C*07:113', 'HLA-C*07:114', 'HLA-C*07:115', 'HLA-C*07:116', 'HLA-C*07:117', 'HLA-C*07:118',
         'HLA-C*07:119', 'HLA-C*07:12', 'HLA-C*07:120', 'HLA-C*07:122', 'HLA-C*07:123', 'HLA-C*07:124', 'HLA-C*07:125', 'HLA-C*07:126',
         'HLA-C*07:127', 'HLA-C*07:128', 'HLA-C*07:129', 'HLA-C*07:13', 'HLA-C*07:130', 'HLA-C*07:131', 'HLA-C*07:132', 'HLA-C*07:133',
         'HLA-C*07:134', 'HLA-C*07:135', 'HLA-C*07:136', 'HLA-C*07:137', 'HLA-C*07:138', 'HLA-C*07:139', 'HLA-C*07:14', 'HLA-C*07:140',
         'HLA-C*07:141', 'HLA-C*07:142', 'HLA-C*07:143', 'HLA-C*07:144', 'HLA-C*07:145', 'HLA-C*07:146', 'HLA-C*07:147', 'HLA-C*07:148',
         'HLA-C*07:149', 'HLA-C*07:15', 'HLA-C*07:16', 'HLA-C*07:17', 'HLA-C*07:18', 'HLA-C*07:19', 'HLA-C*07:20', 'HLA-C*07:21', 'HLA-C*07:22',
         'HLA-C*07:23', 'HLA-C*07:24', 'HLA-C*07:25', 'HLA-C*07:26', 'HLA-C*07:27', 'HLA-C*07:28', 'HLA-C*07:29', 'HLA-C*07:30', 'HLA-C*07:31',
         'HLA-C*07:35', 'HLA-C*07:36', 'HLA-C*07:37', 'HLA-C*07:38', 'HLA-C*07:39', 'HLA-C*07:40', 'HLA-C*07:41', 'HLA-C*07:42', 'HLA-C*07:43',
         'HLA-C*07:44', 'HLA-C*07:45', 'HLA-C*07:46', 'HLA-C*07:47', 'HLA-C*07:48', 'HLA-C*07:49', 'HLA-C*07:50', 'HLA-C*07:51', 'HLA-C*07:52',
         'HLA-C*07:53', 'HLA-C*07:54', 'HLA-C*07:56', 'HLA-C*07:57', 'HLA-C*07:58', 'HLA-C*07:59', 'HLA-C*07:60', 'HLA-C*07:62', 'HLA-C*07:63',
         'HLA-C*07:64', 'HLA-C*07:65', 'HLA-C*07:66', 'HLA-C*07:67', 'HLA-C*07:68', 'HLA-C*07:69', 'HLA-C*07:70', 'HLA-C*07:71', 'HLA-C*07:72',
         'HLA-C*07:73', 'HLA-C*07:74', 'HLA-C*07:75', 'HLA-C*07:76', 'HLA-C*07:77', 'HLA-C*07:78', 'HLA-C*07:79', 'HLA-C*07:80', 'HLA-C*07:81',
         'HLA-C*07:82', 'HLA-C*07:83', 'HLA-C*07:84', 'HLA-C*07:85', 'HLA-C*07:86', 'HLA-C*07:87', 'HLA-C*07:88', 'HLA-C*07:89', 'HLA-C*07:90',
         'HLA-C*07:91', 'HLA-C*07:92', 'HLA-C*07:93', 'HLA-C*07:94', 'HLA-C*07:95', 'HLA-C*07:96', 'HLA-C*07:97', 'HLA-C*07:99', 'HLA-C*08:01',
         'HLA-C*08:02', 'HLA-C*08:03', 'HLA-C*08:04', 'HLA-C*08:05', 'HLA-C*08:06', 'HLA-C*08:07', 'HLA-C*08:08', 'HLA-C*08:09', 'HLA-C*08:10',
         'HLA-C*08:11', 'HLA-C*08:12', 'HLA-C*08:13', 'HLA-C*08:14', 'HLA-C*08:15', 'HLA-C*08:16', 'HLA-C*08:17', 'HLA-C*08:18', 'HLA-C*08:19',
         'HLA-C*08:20', 'HLA-C*08:21', 'HLA-C*08:22', 'HLA-C*08:23', 'HLA-C*08:24', 'HLA-C*08:25', 'HLA-C*08:27', 'HLA-C*08:28', 'HLA-C*08:29',
         'HLA-C*08:30', 'HLA-C*08:31', 'HLA-C*08:32', 'HLA-C*08:33', 'HLA-C*08:34', 'HLA-C*08:35', 'HLA-C*12:02', 'HLA-C*12:03', 'HLA-C*12:04',
         'HLA-C*12:05', 'HLA-C*12:06', 'HLA-C*12:07', 'HLA-C*12:08', 'HLA-C*12:09', 'HLA-C*12:10', 'HLA-C*12:11', 'HLA-C*12:12', 'HLA-C*12:13',
         'HLA-C*12:14', 'HLA-C*12:15', 'HLA-C*12:16', 'HLA-C*12:17', 'HLA-C*12:18', 'HLA-C*12:19', 'HLA-C*12:20', 'HLA-C*12:21', 'HLA-C*12:22',
         'HLA-C*12:23', 'HLA-C*12:24', 'HLA-C*12:25', 'HLA-C*12:26', 'HLA-C*12:27', 'HLA-C*12:28', 'HLA-C*12:29', 'HLA-C*12:30', 'HLA-C*12:31',
         'HLA-C*12:32', 'HLA-C*12:33', 'HLA-C*12:34', 'HLA-C*12:35', 'HLA-C*12:36', 'HLA-C*12:37', 'HLA-C*12:38', 'HLA-C*12:40', 'HLA-C*12:41',
         'HLA-C*12:43', 'HLA-C*12:44', 'HLA-C*14:02', 'HLA-C*14:03', 'HLA-C*14:04', 'HLA-C*14:05', 'HLA-C*14:06', 'HLA-C*14:08', 'HLA-C*14:09',
         'HLA-C*14:10', 'HLA-C*14:11', 'HLA-C*14:12', 'HLA-C*14:13', 'HLA-C*14:14', 'HLA-C*14:15', 'HLA-C*14:16', 'HLA-C*14:17', 'HLA-C*14:18',
         'HLA-C*14:19', 'HLA-C*14:20', 'HLA-C*15:02', 'HLA-C*15:03', 'HLA-C*15:04', 'HLA-C*15:05', 'HLA-C*15:06', 'HLA-C*15:07', 'HLA-C*15:08',
         'HLA-C*15:09', 'HLA-C*15:10', 'HLA-C*15:11', 'HLA-C*15:12', 'HLA-C*15:13', 'HLA-C*15:15', 'HLA-C*15:16', 'HLA-C*15:17', 'HLA-C*15:18',
         'HLA-C*15:19', 'HLA-C*15:20', 'HLA-C*15:21', 'HLA-C*15:22', 'HLA-C*15:23', 'HLA-C*15:24', 'HLA-C*15:25', 'HLA-C*15:26', 'HLA-C*15:27',
         'HLA-C*15:28', 'HLA-C*15:29', 'HLA-C*15:30', 'HLA-C*15:31', 'HLA-C*15:33', 'HLA-C*15:34', 'HLA-C*15:35', 'HLA-C*16:01', 'HLA-C*16:02',
         'HLA-C*16:04', 'HLA-C*16:06', 'HLA-C*16:07', 'HLA-C*16:08', 'HLA-C*16:09', 'HLA-C*16:10', 'HLA-C*16:11', 'HLA-C*16:12', 'HLA-C*16:13',
         'HLA-C*16:14', 'HLA-C*16:15', 'HLA-C*16:17', 'HLA-C*16:18', 'HLA-C*16:19', 'HLA-C*16:20', 'HLA-C*16:21', 'HLA-C*16:22', 'HLA-C*16:23',
         'HLA-C*16:24', 'HLA-C*16:25', 'HLA-C*16:26', 'HLA-C*17:01', 'HLA-C*17:02', 'HLA-C*17:03', 'HLA-C*17:04', 'HLA-C*17:05', 'HLA-C*17:06',
         'HLA-C*17:07', 'HLA-C*18:01', 'HLA-C*18:02', 'HLA-C*18:03', 'HLA-E*01:01', 'HLA-G*01:01', 'HLA-G*01:02', 'HLA-G*01:03', 'HLA-G*01:04',
         'HLA-G*01:06', 'HLA-G*01:07', 'HLA-G*01:08', 'HLA-G*01:09',
         'H-2-Db', 'H-2-Dd', 'H-2-Kb', 'H-2-Kd', 'H-2-Kk', 'H-2-Ld'])
    __version = "2.4"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def supportedAlleles(self):
        """
        A list of valid :class:`~epytope.Core.Allele.Allele` models
        """
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s:%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter = '\t')
        scores = defaultdict(defaultdict)
        alleles = [x for x in next(f) if "HLA" in x]
        # Rank is not supported in command line tool of NetMHCpan 2.4
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCPAN_2_4]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCPAN_2_4 + i])
        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Allele2':...}
        result = {allele: {"Score":(list(scores.values())[j])} for j, allele in enumerate(alleles)}

        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        # can not be determined netmhcpan does not support --version or similar
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools and writes them to file in the specific format

        NO return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))


class NetMHCpan_2_8(AExternalEpitopePrediction):
    """
    Implements the NetMHC binding (in current form for netMHCpan 2.8).
    Supported  MHC alleles currently only restricted to HLA alleles.

    .. note::

        Nielsen, Morten, et al. "NetMHCpan, a method for quantitative predictions of peptide binding to any HLA-A and-B
        locus protein of known sequence." PloS one 2.8 (2007): e796.
    """
    __version = "2.8"
    __supported_length = frozenset([8, 9, 10, 11, 12, 13, 14])
    __name = "netmhcpan"
    __command = "netMHCpan -p {peptides} -a {alleles} {options} -ic50 -xls -xlsfile {out}"
    __alleles = frozenset(
        ['HLA-A*01:01', 'HLA-A*01:02', 'HLA-A*01:03', 'HLA-A*01:06', 'HLA-A*01:07', 'HLA-A*01:08', 'HLA-A*01:09', 'HLA-A*01:10', 'HLA-A*01:12',
         'HLA-A*01:13', 'HLA-A*01:14', 'HLA-A*01:17', 'HLA-A*01:19', 'HLA-A*01:20', 'HLA-A*01:21', 'HLA-A*01:23', 'HLA-A*01:24', 'HLA-A*01:25',
         'HLA-A*01:26', 'HLA-A*01:28', 'HLA-A*01:29', 'HLA-A*01:30', 'HLA-A*01:32', 'HLA-A*01:33', 'HLA-A*01:35', 'HLA-A*01:36', 'HLA-A*01:37',
         'HLA-A*01:38', 'HLA-A*01:39', 'HLA-A*01:40', 'HLA-A*01:41', 'HLA-A*01:42', 'HLA-A*01:43', 'HLA-A*01:44', 'HLA-A*01:45', 'HLA-A*01:46',
         'HLA-A*01:47', 'HLA-A*01:48', 'HLA-A*01:49', 'HLA-A*01:50', 'HLA-A*01:51', 'HLA-A*01:54', 'HLA-A*01:55', 'HLA-A*01:58', 'HLA-A*01:59',
         'HLA-A*01:60', 'HLA-A*01:61', 'HLA-A*01:62', 'HLA-A*01:63', 'HLA-A*01:64', 'HLA-A*01:65', 'HLA-A*01:66', 'HLA-A*02:01', 'HLA-A*02:02',
         'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:05', 'HLA-A*02:06', 'HLA-A*02:07', 'HLA-A*02:08', 'HLA-A*02:09', 'HLA-A*02:10', 'HLA-A*02:101',
         'HLA-A*02:102', 'HLA-A*02:103', 'HLA-A*02:104', 'HLA-A*02:105', 'HLA-A*02:106', 'HLA-A*02:107', 'HLA-A*02:108', 'HLA-A*02:109',
         'HLA-A*02:11', 'HLA-A*02:110', 'HLA-A*02:111', 'HLA-A*02:112', 'HLA-A*02:114', 'HLA-A*02:115', 'HLA-A*02:116', 'HLA-A*02:117',
         'HLA-A*02:118', 'HLA-A*02:119', 'HLA-A*02:12', 'HLA-A*02:120', 'HLA-A*02:121', 'HLA-A*02:122', 'HLA-A*02:123', 'HLA-A*02:124',
         'HLA-A*02:126', 'HLA-A*02:127', 'HLA-A*02:128', 'HLA-A*02:129', 'HLA-A*02:13', 'HLA-A*02:130', 'HLA-A*02:131', 'HLA-A*02:132',
         'HLA-A*02:133', 'HLA-A*02:134', 'HLA-A*02:135', 'HLA-A*02:136', 'HLA-A*02:137', 'HLA-A*02:138', 'HLA-A*02:139', 'HLA-A*02:14',
         'HLA-A*02:140', 'HLA-A*02:141', 'HLA-A*02:142', 'HLA-A*02:143', 'HLA-A*02:144', 'HLA-A*02:145', 'HLA-A*02:146', 'HLA-A*02:147',
         'HLA-A*02:148', 'HLA-A*02:149', 'HLA-A*02:150', 'HLA-A*02:151', 'HLA-A*02:152', 'HLA-A*02:153', 'HLA-A*02:154', 'HLA-A*02:155',
         'HLA-A*02:156', 'HLA-A*02:157', 'HLA-A*02:158', 'HLA-A*02:159', 'HLA-A*02:16', 'HLA-A*02:160', 'HLA-A*02:161', 'HLA-A*02:162',
         'HLA-A*02:163', 'HLA-A*02:164', 'HLA-A*02:165', 'HLA-A*02:166', 'HLA-A*02:167', 'HLA-A*02:168', 'HLA-A*02:169', 'HLA-A*02:17',
         'HLA-A*02:170', 'HLA-A*02:171', 'HLA-A*02:172', 'HLA-A*02:173', 'HLA-A*02:174', 'HLA-A*02:175', 'HLA-A*02:176', 'HLA-A*02:177',
         'HLA-A*02:178', 'HLA-A*02:179', 'HLA-A*02:18', 'HLA-A*02:180', 'HLA-A*02:181', 'HLA-A*02:182', 'HLA-A*02:183', 'HLA-A*02:184',
         'HLA-A*02:185', 'HLA-A*02:186', 'HLA-A*02:187', 'HLA-A*02:188', 'HLA-A*02:189', 'HLA-A*02:19', 'HLA-A*02:190', 'HLA-A*02:191',
         'HLA-A*02:192', 'HLA-A*02:193', 'HLA-A*02:194', 'HLA-A*02:195', 'HLA-A*02:196', 'HLA-A*02:197', 'HLA-A*02:198', 'HLA-A*02:199',
         'HLA-A*02:20', 'HLA-A*02:200', 'HLA-A*02:201', 'HLA-A*02:202', 'HLA-A*02:203', 'HLA-A*02:204', 'HLA-A*02:205', 'HLA-A*02:206',
         'HLA-A*02:207', 'HLA-A*02:208', 'HLA-A*02:209', 'HLA-A*02:21', 'HLA-A*02:210', 'HLA-A*02:211', 'HLA-A*02:212', 'HLA-A*02:213',
         'HLA-A*02:214', 'HLA-A*02:215', 'HLA-A*02:216', 'HLA-A*02:217', 'HLA-A*02:218', 'HLA-A*02:219', 'HLA-A*02:22', 'HLA-A*02:220',
         'HLA-A*02:221', 'HLA-A*02:224', 'HLA-A*02:228', 'HLA-A*02:229', 'HLA-A*02:230', 'HLA-A*02:231', 'HLA-A*02:232', 'HLA-A*02:233',
         'HLA-A*02:234', 'HLA-A*02:235', 'HLA-A*02:236', 'HLA-A*02:237', 'HLA-A*02:238', 'HLA-A*02:239', 'HLA-A*02:24', 'HLA-A*02:240',
         'HLA-A*02:241', 'HLA-A*02:242', 'HLA-A*02:243', 'HLA-A*02:244', 'HLA-A*02:245', 'HLA-A*02:246', 'HLA-A*02:247', 'HLA-A*02:248',
         'HLA-A*02:249', 'HLA-A*02:25', 'HLA-A*02:251', 'HLA-A*02:252', 'HLA-A*02:253', 'HLA-A*02:254', 'HLA-A*02:255', 'HLA-A*02:256',
         'HLA-A*02:257', 'HLA-A*02:258', 'HLA-A*02:259', 'HLA-A*02:26', 'HLA-A*02:260', 'HLA-A*02:261', 'HLA-A*02:262', 'HLA-A*02:263',
         'HLA-A*02:264', 'HLA-A*02:265', 'HLA-A*02:266', 'HLA-A*02:27', 'HLA-A*02:28', 'HLA-A*02:29', 'HLA-A*02:30', 'HLA-A*02:31', 'HLA-A*02:33',
         'HLA-A*02:34', 'HLA-A*02:35', 'HLA-A*02:36', 'HLA-A*02:37', 'HLA-A*02:38', 'HLA-A*02:39', 'HLA-A*02:40', 'HLA-A*02:41', 'HLA-A*02:42',
         'HLA-A*02:44', 'HLA-A*02:45', 'HLA-A*02:46', 'HLA-A*02:47', 'HLA-A*02:48', 'HLA-A*02:49', 'HLA-A*02:50', 'HLA-A*02:51', 'HLA-A*02:52',
         'HLA-A*02:54', 'HLA-A*02:55', 'HLA-A*02:56', 'HLA-A*02:57', 'HLA-A*02:58', 'HLA-A*02:59', 'HLA-A*02:60', 'HLA-A*02:61', 'HLA-A*02:62',
         'HLA-A*02:63', 'HLA-A*02:64', 'HLA-A*02:65', 'HLA-A*02:66', 'HLA-A*02:67', 'HLA-A*02:68', 'HLA-A*02:69', 'HLA-A*02:70', 'HLA-A*02:71',
         'HLA-A*02:72', 'HLA-A*02:73', 'HLA-A*02:74', 'HLA-A*02:75', 'HLA-A*02:76', 'HLA-A*02:77', 'HLA-A*02:78', 'HLA-A*02:79', 'HLA-A*02:80',
         'HLA-A*02:81', 'HLA-A*02:84', 'HLA-A*02:85', 'HLA-A*02:86', 'HLA-A*02:87', 'HLA-A*02:89', 'HLA-A*02:90', 'HLA-A*02:91', 'HLA-A*02:92',
         'HLA-A*02:93', 'HLA-A*02:95', 'HLA-A*02:96', 'HLA-A*02:97', 'HLA-A*02:99', 'HLA-A*03:01', 'HLA-A*03:02', 'HLA-A*03:04', 'HLA-A*03:05',
         'HLA-A*03:06', 'HLA-A*03:07', 'HLA-A*03:08', 'HLA-A*03:09', 'HLA-A*03:10', 'HLA-A*03:12', 'HLA-A*03:13', 'HLA-A*03:14', 'HLA-A*03:15',
         'HLA-A*03:16', 'HLA-A*03:17', 'HLA-A*03:18', 'HLA-A*03:19', 'HLA-A*03:20', 'HLA-A*03:22', 'HLA-A*03:23', 'HLA-A*03:24', 'HLA-A*03:25',
         'HLA-A*03:26', 'HLA-A*03:27', 'HLA-A*03:28', 'HLA-A*03:29', 'HLA-A*03:30', 'HLA-A*03:31', 'HLA-A*03:32', 'HLA-A*03:33', 'HLA-A*03:34',
         'HLA-A*03:35', 'HLA-A*03:37', 'HLA-A*03:38', 'HLA-A*03:39', 'HLA-A*03:40', 'HLA-A*03:41', 'HLA-A*03:42', 'HLA-A*03:43', 'HLA-A*03:44',
         'HLA-A*03:45', 'HLA-A*03:46', 'HLA-A*03:47', 'HLA-A*03:48', 'HLA-A*03:49', 'HLA-A*03:50', 'HLA-A*03:51', 'HLA-A*03:52', 'HLA-A*03:53',
         'HLA-A*03:54', 'HLA-A*03:55', 'HLA-A*03:56', 'HLA-A*03:57', 'HLA-A*03:58', 'HLA-A*03:59', 'HLA-A*03:60', 'HLA-A*03:61', 'HLA-A*03:62',
         'HLA-A*03:63', 'HLA-A*03:64', 'HLA-A*03:65', 'HLA-A*03:66', 'HLA-A*03:67', 'HLA-A*03:70', 'HLA-A*03:71', 'HLA-A*03:72', 'HLA-A*03:73',
         'HLA-A*03:74', 'HLA-A*03:75', 'HLA-A*03:76', 'HLA-A*03:77', 'HLA-A*03:78', 'HLA-A*03:79', 'HLA-A*03:80', 'HLA-A*03:81', 'HLA-A*03:82',
         'HLA-A*11:01', 'HLA-A*11:02', 'HLA-A*11:03', 'HLA-A*11:04', 'HLA-A*11:05', 'HLA-A*11:06', 'HLA-A*11:07', 'HLA-A*11:08', 'HLA-A*11:09',
         'HLA-A*11:10', 'HLA-A*11:11', 'HLA-A*11:12', 'HLA-A*11:13', 'HLA-A*11:14', 'HLA-A*11:15', 'HLA-A*11:16', 'HLA-A*11:17', 'HLA-A*11:18',
         'HLA-A*11:19', 'HLA-A*11:20', 'HLA-A*11:22', 'HLA-A*11:23', 'HLA-A*11:24', 'HLA-A*11:25', 'HLA-A*11:26', 'HLA-A*11:27', 'HLA-A*11:29',
         'HLA-A*11:30', 'HLA-A*11:31', 'HLA-A*11:32', 'HLA-A*11:33', 'HLA-A*11:34', 'HLA-A*11:35', 'HLA-A*11:36', 'HLA-A*11:37', 'HLA-A*11:38',
         'HLA-A*11:39', 'HLA-A*11:40', 'HLA-A*11:41', 'HLA-A*11:42', 'HLA-A*11:43', 'HLA-A*11:44', 'HLA-A*11:45', 'HLA-A*11:46', 'HLA-A*11:47',
         'HLA-A*11:48', 'HLA-A*11:49', 'HLA-A*11:51', 'HLA-A*11:53', 'HLA-A*11:54', 'HLA-A*11:55', 'HLA-A*11:56', 'HLA-A*11:57', 'HLA-A*11:58',
         'HLA-A*11:59', 'HLA-A*11:60', 'HLA-A*11:61', 'HLA-A*11:62', 'HLA-A*11:63', 'HLA-A*11:64', 'HLA-A*23:01', 'HLA-A*23:02', 'HLA-A*23:03',
         'HLA-A*23:04', 'HLA-A*23:05', 'HLA-A*23:06', 'HLA-A*23:09', 'HLA-A*23:10', 'HLA-A*23:12', 'HLA-A*23:13', 'HLA-A*23:14', 'HLA-A*23:15',
         'HLA-A*23:16', 'HLA-A*23:17', 'HLA-A*23:18', 'HLA-A*23:20', 'HLA-A*23:21', 'HLA-A*23:22', 'HLA-A*23:23', 'HLA-A*23:24', 'HLA-A*23:25',
         'HLA-A*23:26', 'HLA-A*24:02', 'HLA-A*24:03', 'HLA-A*24:04', 'HLA-A*24:05', 'HLA-A*24:06', 'HLA-A*24:07', 'HLA-A*24:08', 'HLA-A*24:10',
         'HLA-A*24:100', 'HLA-A*24:101', 'HLA-A*24:102', 'HLA-A*24:103', 'HLA-A*24:104', 'HLA-A*24:105', 'HLA-A*24:106', 'HLA-A*24:107',
         'HLA-A*24:108', 'HLA-A*24:109', 'HLA-A*24:110', 'HLA-A*24:111', 'HLA-A*24:112', 'HLA-A*24:113', 'HLA-A*24:114', 'HLA-A*24:115',
         'HLA-A*24:116', 'HLA-A*24:117', 'HLA-A*24:118', 'HLA-A*24:119', 'HLA-A*24:120', 'HLA-A*24:121', 'HLA-A*24:122', 'HLA-A*24:123',
         'HLA-A*24:124', 'HLA-A*24:125', 'HLA-A*24:126', 'HLA-A*24:127', 'HLA-A*24:128', 'HLA-A*24:129', 'HLA-A*24:13', 'HLA-A*24:130',
         'HLA-A*24:131', 'HLA-A*24:133', 'HLA-A*24:134', 'HLA-A*24:135', 'HLA-A*24:136', 'HLA-A*24:137', 'HLA-A*24:138', 'HLA-A*24:139',
         'HLA-A*24:14', 'HLA-A*24:140', 'HLA-A*24:141', 'HLA-A*24:142', 'HLA-A*24:143', 'HLA-A*24:144', 'HLA-A*24:15', 'HLA-A*24:17', 'HLA-A*24:18',
         'HLA-A*24:19', 'HLA-A*24:20', 'HLA-A*24:21', 'HLA-A*24:22', 'HLA-A*24:23', 'HLA-A*24:24', 'HLA-A*24:25', 'HLA-A*24:26', 'HLA-A*24:27',
         'HLA-A*24:28', 'HLA-A*24:29', 'HLA-A*24:30', 'HLA-A*24:31', 'HLA-A*24:32', 'HLA-A*24:33', 'HLA-A*24:34', 'HLA-A*24:35', 'HLA-A*24:37',
         'HLA-A*24:38', 'HLA-A*24:39', 'HLA-A*24:41', 'HLA-A*24:42', 'HLA-A*24:43', 'HLA-A*24:44', 'HLA-A*24:46', 'HLA-A*24:47', 'HLA-A*24:49',
         'HLA-A*24:50', 'HLA-A*24:51', 'HLA-A*24:52', 'HLA-A*24:53', 'HLA-A*24:54', 'HLA-A*24:55', 'HLA-A*24:56', 'HLA-A*24:57', 'HLA-A*24:58',
         'HLA-A*24:59', 'HLA-A*24:61', 'HLA-A*24:62', 'HLA-A*24:63', 'HLA-A*24:64', 'HLA-A*24:66', 'HLA-A*24:67', 'HLA-A*24:68', 'HLA-A*24:69',
         'HLA-A*24:70', 'HLA-A*24:71', 'HLA-A*24:72', 'HLA-A*24:73', 'HLA-A*24:74', 'HLA-A*24:75', 'HLA-A*24:76', 'HLA-A*24:77', 'HLA-A*24:78',
         'HLA-A*24:79', 'HLA-A*24:80', 'HLA-A*24:81', 'HLA-A*24:82', 'HLA-A*24:85', 'HLA-A*24:87', 'HLA-A*24:88', 'HLA-A*24:89', 'HLA-A*24:91',
         'HLA-A*24:92', 'HLA-A*24:93', 'HLA-A*24:94', 'HLA-A*24:95', 'HLA-A*24:96', 'HLA-A*24:97', 'HLA-A*24:98', 'HLA-A*24:99', 'HLA-A*25:01',
         'HLA-A*25:02', 'HLA-A*25:03', 'HLA-A*25:04', 'HLA-A*25:05', 'HLA-A*25:06', 'HLA-A*25:07', 'HLA-A*25:08', 'HLA-A*25:09', 'HLA-A*25:10',
         'HLA-A*25:11', 'HLA-A*25:13', 'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*26:04', 'HLA-A*26:05', 'HLA-A*26:06', 'HLA-A*26:07',
         'HLA-A*26:08', 'HLA-A*26:09', 'HLA-A*26:10', 'HLA-A*26:12', 'HLA-A*26:13', 'HLA-A*26:14', 'HLA-A*26:15', 'HLA-A*26:16', 'HLA-A*26:17',
         'HLA-A*26:18', 'HLA-A*26:19', 'HLA-A*26:20', 'HLA-A*26:21', 'HLA-A*26:22', 'HLA-A*26:23', 'HLA-A*26:24', 'HLA-A*26:26', 'HLA-A*26:27',
         'HLA-A*26:28', 'HLA-A*26:29', 'HLA-A*26:30', 'HLA-A*26:31', 'HLA-A*26:32', 'HLA-A*26:33', 'HLA-A*26:34', 'HLA-A*26:35', 'HLA-A*26:36',
         'HLA-A*26:37', 'HLA-A*26:38', 'HLA-A*26:39', 'HLA-A*26:40', 'HLA-A*26:41', 'HLA-A*26:42', 'HLA-A*26:43', 'HLA-A*26:45', 'HLA-A*26:46',
         'HLA-A*26:47', 'HLA-A*26:48', 'HLA-A*26:49', 'HLA-A*26:50', 'HLA-A*29:01', 'HLA-A*29:02', 'HLA-A*29:03', 'HLA-A*29:04', 'HLA-A*29:05',
         'HLA-A*29:06', 'HLA-A*29:07', 'HLA-A*29:09', 'HLA-A*29:10', 'HLA-A*29:11', 'HLA-A*29:12', 'HLA-A*29:13', 'HLA-A*29:14', 'HLA-A*29:15',
         'HLA-A*29:16', 'HLA-A*29:17', 'HLA-A*29:18', 'HLA-A*29:19', 'HLA-A*29:20', 'HLA-A*29:21', 'HLA-A*29:22', 'HLA-A*30:01', 'HLA-A*30:02',
         'HLA-A*30:03', 'HLA-A*30:04', 'HLA-A*30:06', 'HLA-A*30:07', 'HLA-A*30:08', 'HLA-A*30:09', 'HLA-A*30:10', 'HLA-A*30:11', 'HLA-A*30:12',
         'HLA-A*30:13', 'HLA-A*30:15', 'HLA-A*30:16', 'HLA-A*30:17', 'HLA-A*30:18', 'HLA-A*30:19', 'HLA-A*30:20', 'HLA-A*30:22', 'HLA-A*30:23',
         'HLA-A*30:24', 'HLA-A*30:25', 'HLA-A*30:26', 'HLA-A*30:28', 'HLA-A*30:29', 'HLA-A*30:30', 'HLA-A*30:31', 'HLA-A*30:32', 'HLA-A*30:33',
         'HLA-A*30:34', 'HLA-A*30:35', 'HLA-A*30:36', 'HLA-A*30:37', 'HLA-A*30:38', 'HLA-A*30:39', 'HLA-A*30:40', 'HLA-A*30:41', 'HLA-A*31:01',
         'HLA-A*31:02', 'HLA-A*31:03', 'HLA-A*31:04', 'HLA-A*31:05', 'HLA-A*31:06', 'HLA-A*31:07', 'HLA-A*31:08', 'HLA-A*31:09', 'HLA-A*31:10',
         'HLA-A*31:11', 'HLA-A*31:12', 'HLA-A*31:13', 'HLA-A*31:15', 'HLA-A*31:16', 'HLA-A*31:17', 'HLA-A*31:18', 'HLA-A*31:19', 'HLA-A*31:20',
         'HLA-A*31:21', 'HLA-A*31:22', 'HLA-A*31:23', 'HLA-A*31:24', 'HLA-A*31:25', 'HLA-A*31:26', 'HLA-A*31:27', 'HLA-A*31:28', 'HLA-A*31:29',
         'HLA-A*31:30', 'HLA-A*31:31', 'HLA-A*31:32', 'HLA-A*31:33', 'HLA-A*31:34', 'HLA-A*31:35', 'HLA-A*31:36', 'HLA-A*31:37', 'HLA-A*32:01',
         'HLA-A*32:02', 'HLA-A*32:03', 'HLA-A*32:04', 'HLA-A*32:05', 'HLA-A*32:06', 'HLA-A*32:07', 'HLA-A*32:08', 'HLA-A*32:09', 'HLA-A*32:10',
         'HLA-A*32:12', 'HLA-A*32:13', 'HLA-A*32:14', 'HLA-A*32:15', 'HLA-A*32:16', 'HLA-A*32:17', 'HLA-A*32:18', 'HLA-A*32:20', 'HLA-A*32:21',
         'HLA-A*32:22', 'HLA-A*32:23', 'HLA-A*32:24', 'HLA-A*32:25', 'HLA-A*33:01', 'HLA-A*33:03', 'HLA-A*33:04', 'HLA-A*33:05', 'HLA-A*33:06',
         'HLA-A*33:07', 'HLA-A*33:08', 'HLA-A*33:09', 'HLA-A*33:10', 'HLA-A*33:11', 'HLA-A*33:12', 'HLA-A*33:13', 'HLA-A*33:14', 'HLA-A*33:15',
         'HLA-A*33:16', 'HLA-A*33:17', 'HLA-A*33:18', 'HLA-A*33:19', 'HLA-A*33:20', 'HLA-A*33:21', 'HLA-A*33:22', 'HLA-A*33:23', 'HLA-A*33:24',
         'HLA-A*33:25', 'HLA-A*33:26', 'HLA-A*33:27', 'HLA-A*33:28', 'HLA-A*33:29', 'HLA-A*33:30', 'HLA-A*33:31', 'HLA-A*34:01', 'HLA-A*34:02',
         'HLA-A*34:03', 'HLA-A*34:04', 'HLA-A*34:05', 'HLA-A*34:06', 'HLA-A*34:07', 'HLA-A*34:08', 'HLA-A*36:01', 'HLA-A*36:02', 'HLA-A*36:03',
         'HLA-A*36:04', 'HLA-A*36:05', 'HLA-A*43:01', 'HLA-A*66:01', 'HLA-A*66:02', 'HLA-A*66:03', 'HLA-A*66:04', 'HLA-A*66:05', 'HLA-A*66:06',
         'HLA-A*66:07', 'HLA-A*66:08', 'HLA-A*66:09', 'HLA-A*66:10', 'HLA-A*66:11', 'HLA-A*66:12', 'HLA-A*66:13', 'HLA-A*66:14', 'HLA-A*66:15',
         'HLA-A*68:01', 'HLA-A*68:02', 'HLA-A*68:03', 'HLA-A*68:04', 'HLA-A*68:05', 'HLA-A*68:06', 'HLA-A*68:07', 'HLA-A*68:08', 'HLA-A*68:09',
         'HLA-A*68:10', 'HLA-A*68:12', 'HLA-A*68:13', 'HLA-A*68:14', 'HLA-A*68:15', 'HLA-A*68:16', 'HLA-A*68:17', 'HLA-A*68:19', 'HLA-A*68:20',
         'HLA-A*68:21', 'HLA-A*68:22', 'HLA-A*68:23', 'HLA-A*68:24', 'HLA-A*68:25', 'HLA-A*68:26', 'HLA-A*68:27', 'HLA-A*68:28', 'HLA-A*68:29',
         'HLA-A*68:30', 'HLA-A*68:31', 'HLA-A*68:32', 'HLA-A*68:33', 'HLA-A*68:34', 'HLA-A*68:35', 'HLA-A*68:36', 'HLA-A*68:37', 'HLA-A*68:38',
         'HLA-A*68:39', 'HLA-A*68:40', 'HLA-A*68:41', 'HLA-A*68:42', 'HLA-A*68:43', 'HLA-A*68:44', 'HLA-A*68:45', 'HLA-A*68:46', 'HLA-A*68:47',
         'HLA-A*68:48', 'HLA-A*68:50', 'HLA-A*68:51', 'HLA-A*68:52', 'HLA-A*68:53', 'HLA-A*68:54', 'HLA-A*69:01', 'HLA-A*74:01', 'HLA-A*74:02',
         'HLA-A*74:03', 'HLA-A*74:04', 'HLA-A*74:05', 'HLA-A*74:06', 'HLA-A*74:07', 'HLA-A*74:08', 'HLA-A*74:09', 'HLA-A*74:10', 'HLA-A*74:11',
         'HLA-A*74:13', 'HLA-A*80:01', 'HLA-A*80:02', 'HLA-B*07:02', 'HLA-B*07:03', 'HLA-B*07:04', 'HLA-B*07:05', 'HLA-B*07:06', 'HLA-B*07:07',
         'HLA-B*07:08', 'HLA-B*07:09', 'HLA-B*07:10', 'HLA-B*07:100', 'HLA-B*07:101', 'HLA-B*07:102', 'HLA-B*07:103', 'HLA-B*07:104',
         'HLA-B*07:105', 'HLA-B*07:106', 'HLA-B*07:107', 'HLA-B*07:108', 'HLA-B*07:109', 'HLA-B*07:11', 'HLA-B*07:110', 'HLA-B*07:112',
         'HLA-B*07:113', 'HLA-B*07:114', 'HLA-B*07:115', 'HLA-B*07:12', 'HLA-B*07:13', 'HLA-B*07:14', 'HLA-B*07:15', 'HLA-B*07:16', 'HLA-B*07:17',
         'HLA-B*07:18', 'HLA-B*07:19', 'HLA-B*07:20', 'HLA-B*07:21', 'HLA-B*07:22', 'HLA-B*07:23', 'HLA-B*07:24', 'HLA-B*07:25', 'HLA-B*07:26',
         'HLA-B*07:27', 'HLA-B*07:28', 'HLA-B*07:29', 'HLA-B*07:30', 'HLA-B*07:31', 'HLA-B*07:32', 'HLA-B*07:33', 'HLA-B*07:34', 'HLA-B*07:35',
         'HLA-B*07:36', 'HLA-B*07:37', 'HLA-B*07:38', 'HLA-B*07:39', 'HLA-B*07:40', 'HLA-B*07:41', 'HLA-B*07:42', 'HLA-B*07:43', 'HLA-B*07:44',
         'HLA-B*07:45', 'HLA-B*07:46', 'HLA-B*07:47', 'HLA-B*07:48', 'HLA-B*07:50', 'HLA-B*07:51', 'HLA-B*07:52', 'HLA-B*07:53', 'HLA-B*07:54',
         'HLA-B*07:55', 'HLA-B*07:56', 'HLA-B*07:57', 'HLA-B*07:58', 'HLA-B*07:59', 'HLA-B*07:60', 'HLA-B*07:61', 'HLA-B*07:62', 'HLA-B*07:63',
         'HLA-B*07:64', 'HLA-B*07:65', 'HLA-B*07:66', 'HLA-B*07:68', 'HLA-B*07:69', 'HLA-B*07:70', 'HLA-B*07:71', 'HLA-B*07:72', 'HLA-B*07:73',
         'HLA-B*07:74', 'HLA-B*07:75', 'HLA-B*07:76', 'HLA-B*07:77', 'HLA-B*07:78', 'HLA-B*07:79', 'HLA-B*07:80', 'HLA-B*07:81', 'HLA-B*07:82',
         'HLA-B*07:83', 'HLA-B*07:84', 'HLA-B*07:85', 'HLA-B*07:86', 'HLA-B*07:87', 'HLA-B*07:88', 'HLA-B*07:89', 'HLA-B*07:90', 'HLA-B*07:91',
         'HLA-B*07:92', 'HLA-B*07:93', 'HLA-B*07:94', 'HLA-B*07:95', 'HLA-B*07:96', 'HLA-B*07:97', 'HLA-B*07:98', 'HLA-B*07:99', 'HLA-B*08:01',
         'HLA-B*08:02', 'HLA-B*08:03', 'HLA-B*08:04', 'HLA-B*08:05', 'HLA-B*08:07', 'HLA-B*08:09', 'HLA-B*08:10', 'HLA-B*08:11', 'HLA-B*08:12',
         'HLA-B*08:13', 'HLA-B*08:14', 'HLA-B*08:15', 'HLA-B*08:16', 'HLA-B*08:17', 'HLA-B*08:18', 'HLA-B*08:20', 'HLA-B*08:21', 'HLA-B*08:22',
         'HLA-B*08:23', 'HLA-B*08:24', 'HLA-B*08:25', 'HLA-B*08:26', 'HLA-B*08:27', 'HLA-B*08:28', 'HLA-B*08:29', 'HLA-B*08:31', 'HLA-B*08:32',
         'HLA-B*08:33', 'HLA-B*08:34', 'HLA-B*08:35', 'HLA-B*08:36', 'HLA-B*08:37', 'HLA-B*08:38', 'HLA-B*08:39', 'HLA-B*08:40', 'HLA-B*08:41',
         'HLA-B*08:42', 'HLA-B*08:43', 'HLA-B*08:44', 'HLA-B*08:45', 'HLA-B*08:46', 'HLA-B*08:47', 'HLA-B*08:48', 'HLA-B*08:49', 'HLA-B*08:50',
         'HLA-B*08:51', 'HLA-B*08:52', 'HLA-B*08:53', 'HLA-B*08:54', 'HLA-B*08:55', 'HLA-B*08:56', 'HLA-B*08:57', 'HLA-B*08:58', 'HLA-B*08:59',
         'HLA-B*08:60', 'HLA-B*08:61', 'HLA-B*08:62', 'HLA-B*13:01', 'HLA-B*13:02', 'HLA-B*13:03', 'HLA-B*13:04', 'HLA-B*13:06', 'HLA-B*13:09',
         'HLA-B*13:10', 'HLA-B*13:11', 'HLA-B*13:12', 'HLA-B*13:13', 'HLA-B*13:14', 'HLA-B*13:15', 'HLA-B*13:16', 'HLA-B*13:17', 'HLA-B*13:18',
         'HLA-B*13:19', 'HLA-B*13:20', 'HLA-B*13:21', 'HLA-B*13:22', 'HLA-B*13:23', 'HLA-B*13:25', 'HLA-B*13:26', 'HLA-B*13:27', 'HLA-B*13:28',
         'HLA-B*13:29', 'HLA-B*13:30', 'HLA-B*13:31', 'HLA-B*13:32', 'HLA-B*13:33', 'HLA-B*13:34', 'HLA-B*13:35', 'HLA-B*13:36', 'HLA-B*13:37',
         'HLA-B*13:38', 'HLA-B*13:39', 'HLA-B*14:01', 'HLA-B*14:02', 'HLA-B*14:03', 'HLA-B*14:04', 'HLA-B*14:05', 'HLA-B*14:06', 'HLA-B*14:08',
         'HLA-B*14:09', 'HLA-B*14:10', 'HLA-B*14:11', 'HLA-B*14:12', 'HLA-B*14:13', 'HLA-B*14:14', 'HLA-B*14:15', 'HLA-B*14:16', 'HLA-B*14:17',
         'HLA-B*14:18', 'HLA-B*15:01', 'HLA-B*15:02', 'HLA-B*15:03', 'HLA-B*15:04', 'HLA-B*15:05', 'HLA-B*15:06', 'HLA-B*15:07', 'HLA-B*15:08',
         'HLA-B*15:09', 'HLA-B*15:10', 'HLA-B*15:101', 'HLA-B*15:102', 'HLA-B*15:103', 'HLA-B*15:104', 'HLA-B*15:105', 'HLA-B*15:106',
         'HLA-B*15:107', 'HLA-B*15:108', 'HLA-B*15:109', 'HLA-B*15:11', 'HLA-B*15:110', 'HLA-B*15:112', 'HLA-B*15:113', 'HLA-B*15:114',
         'HLA-B*15:115', 'HLA-B*15:116', 'HLA-B*15:117', 'HLA-B*15:118', 'HLA-B*15:119', 'HLA-B*15:12', 'HLA-B*15:120', 'HLA-B*15:121',
         'HLA-B*15:122', 'HLA-B*15:123', 'HLA-B*15:124', 'HLA-B*15:125', 'HLA-B*15:126', 'HLA-B*15:127', 'HLA-B*15:128', 'HLA-B*15:129',
         'HLA-B*15:13', 'HLA-B*15:131', 'HLA-B*15:132', 'HLA-B*15:133', 'HLA-B*15:134', 'HLA-B*15:135', 'HLA-B*15:136', 'HLA-B*15:137',
         'HLA-B*15:138', 'HLA-B*15:139', 'HLA-B*15:14', 'HLA-B*15:140', 'HLA-B*15:141', 'HLA-B*15:142', 'HLA-B*15:143', 'HLA-B*15:144',
         'HLA-B*15:145', 'HLA-B*15:146', 'HLA-B*15:147', 'HLA-B*15:148', 'HLA-B*15:15', 'HLA-B*15:150', 'HLA-B*15:151', 'HLA-B*15:152',
         'HLA-B*15:153', 'HLA-B*15:154', 'HLA-B*15:155', 'HLA-B*15:156', 'HLA-B*15:157', 'HLA-B*15:158', 'HLA-B*15:159', 'HLA-B*15:16',
         'HLA-B*15:160', 'HLA-B*15:161', 'HLA-B*15:162', 'HLA-B*15:163', 'HLA-B*15:164', 'HLA-B*15:165', 'HLA-B*15:166', 'HLA-B*15:167',
         'HLA-B*15:168', 'HLA-B*15:169', 'HLA-B*15:17', 'HLA-B*15:170', 'HLA-B*15:171', 'HLA-B*15:172', 'HLA-B*15:173', 'HLA-B*15:174',
         'HLA-B*15:175', 'HLA-B*15:176', 'HLA-B*15:177', 'HLA-B*15:178', 'HLA-B*15:179', 'HLA-B*15:18', 'HLA-B*15:180', 'HLA-B*15:183',
         'HLA-B*15:184', 'HLA-B*15:185', 'HLA-B*15:186', 'HLA-B*15:187', 'HLA-B*15:188', 'HLA-B*15:189', 'HLA-B*15:19', 'HLA-B*15:191',
         'HLA-B*15:192', 'HLA-B*15:193', 'HLA-B*15:194', 'HLA-B*15:195', 'HLA-B*15:196', 'HLA-B*15:197', 'HLA-B*15:198', 'HLA-B*15:199',
         'HLA-B*15:20', 'HLA-B*15:200', 'HLA-B*15:201', 'HLA-B*15:202', 'HLA-B*15:21', 'HLA-B*15:23', 'HLA-B*15:24', 'HLA-B*15:25', 'HLA-B*15:27',
         'HLA-B*15:28', 'HLA-B*15:29', 'HLA-B*15:30', 'HLA-B*15:31', 'HLA-B*15:32', 'HLA-B*15:33', 'HLA-B*15:34', 'HLA-B*15:35', 'HLA-B*15:36',
         'HLA-B*15:37', 'HLA-B*15:38', 'HLA-B*15:39', 'HLA-B*15:40', 'HLA-B*15:42', 'HLA-B*15:43', 'HLA-B*15:44', 'HLA-B*15:45', 'HLA-B*15:46',
         'HLA-B*15:47', 'HLA-B*15:48', 'HLA-B*15:49', 'HLA-B*15:50', 'HLA-B*15:51', 'HLA-B*15:52', 'HLA-B*15:53', 'HLA-B*15:54', 'HLA-B*15:55',
         'HLA-B*15:56', 'HLA-B*15:57', 'HLA-B*15:58', 'HLA-B*15:60', 'HLA-B*15:61', 'HLA-B*15:62', 'HLA-B*15:63', 'HLA-B*15:64', 'HLA-B*15:65',
         'HLA-B*15:66', 'HLA-B*15:67', 'HLA-B*15:68', 'HLA-B*15:69', 'HLA-B*15:70', 'HLA-B*15:71', 'HLA-B*15:72', 'HLA-B*15:73', 'HLA-B*15:74',
         'HLA-B*15:75', 'HLA-B*15:76', 'HLA-B*15:77', 'HLA-B*15:78', 'HLA-B*15:80', 'HLA-B*15:81', 'HLA-B*15:82', 'HLA-B*15:83', 'HLA-B*15:84',
         'HLA-B*15:85', 'HLA-B*15:86', 'HLA-B*15:87', 'HLA-B*15:88', 'HLA-B*15:89', 'HLA-B*15:90', 'HLA-B*15:91', 'HLA-B*15:92', 'HLA-B*15:93',
         'HLA-B*15:95', 'HLA-B*15:96', 'HLA-B*15:97', 'HLA-B*15:98', 'HLA-B*15:99', 'HLA-B*18:01', 'HLA-B*18:02', 'HLA-B*18:03', 'HLA-B*18:04',
         'HLA-B*18:05', 'HLA-B*18:06', 'HLA-B*18:07', 'HLA-B*18:08', 'HLA-B*18:09', 'HLA-B*18:10', 'HLA-B*18:11', 'HLA-B*18:12', 'HLA-B*18:13',
         'HLA-B*18:14', 'HLA-B*18:15', 'HLA-B*18:18', 'HLA-B*18:19', 'HLA-B*18:20', 'HLA-B*18:21', 'HLA-B*18:22', 'HLA-B*18:24', 'HLA-B*18:25',
         'HLA-B*18:26', 'HLA-B*18:27', 'HLA-B*18:28', 'HLA-B*18:29', 'HLA-B*18:30', 'HLA-B*18:31', 'HLA-B*18:32', 'HLA-B*18:33', 'HLA-B*18:34',
         'HLA-B*18:35', 'HLA-B*18:36', 'HLA-B*18:37', 'HLA-B*18:38', 'HLA-B*18:39', 'HLA-B*18:40', 'HLA-B*18:41', 'HLA-B*18:42', 'HLA-B*18:43',
         'HLA-B*18:44', 'HLA-B*18:45', 'HLA-B*18:46', 'HLA-B*18:47', 'HLA-B*18:48', 'HLA-B*18:49', 'HLA-B*18:50', 'HLA-B*27:01', 'HLA-B*27:02',
         'HLA-B*27:03', 'HLA-B*27:04', 'HLA-B*27:05', 'HLA-B*27:06', 'HLA-B*27:07', 'HLA-B*27:08', 'HLA-B*27:09', 'HLA-B*27:10', 'HLA-B*27:11',
         'HLA-B*27:12', 'HLA-B*27:13', 'HLA-B*27:14', 'HLA-B*27:15', 'HLA-B*27:16', 'HLA-B*27:17', 'HLA-B*27:18', 'HLA-B*27:19', 'HLA-B*27:20',
         'HLA-B*27:21', 'HLA-B*27:23', 'HLA-B*27:24', 'HLA-B*27:25', 'HLA-B*27:26', 'HLA-B*27:27', 'HLA-B*27:28', 'HLA-B*27:29', 'HLA-B*27:30',
         'HLA-B*27:31', 'HLA-B*27:32', 'HLA-B*27:33', 'HLA-B*27:34', 'HLA-B*27:35', 'HLA-B*27:36', 'HLA-B*27:37', 'HLA-B*27:38', 'HLA-B*27:39',
         'HLA-B*27:40', 'HLA-B*27:41', 'HLA-B*27:42', 'HLA-B*27:43', 'HLA-B*27:44', 'HLA-B*27:45', 'HLA-B*27:46', 'HLA-B*27:47', 'HLA-B*27:48',
         'HLA-B*27:49', 'HLA-B*27:50', 'HLA-B*27:51', 'HLA-B*27:52', 'HLA-B*27:53', 'HLA-B*27:54', 'HLA-B*27:55', 'HLA-B*27:56', 'HLA-B*27:57',
         'HLA-B*27:58', 'HLA-B*27:60', 'HLA-B*27:61', 'HLA-B*27:62', 'HLA-B*27:63', 'HLA-B*27:67', 'HLA-B*27:68', 'HLA-B*27:69', 'HLA-B*35:01',
         'HLA-B*35:02', 'HLA-B*35:03', 'HLA-B*35:04', 'HLA-B*35:05', 'HLA-B*35:06', 'HLA-B*35:07', 'HLA-B*35:08', 'HLA-B*35:09', 'HLA-B*35:10',
         'HLA-B*35:100', 'HLA-B*35:101', 'HLA-B*35:102', 'HLA-B*35:103', 'HLA-B*35:104', 'HLA-B*35:105', 'HLA-B*35:106', 'HLA-B*35:107',
         'HLA-B*35:108', 'HLA-B*35:109', 'HLA-B*35:11', 'HLA-B*35:110', 'HLA-B*35:111', 'HLA-B*35:112', 'HLA-B*35:113', 'HLA-B*35:114',
         'HLA-B*35:115', 'HLA-B*35:116', 'HLA-B*35:117', 'HLA-B*35:118', 'HLA-B*35:119', 'HLA-B*35:12', 'HLA-B*35:120', 'HLA-B*35:121',
         'HLA-B*35:122', 'HLA-B*35:123', 'HLA-B*35:124', 'HLA-B*35:125', 'HLA-B*35:126', 'HLA-B*35:127', 'HLA-B*35:128', 'HLA-B*35:13',
         'HLA-B*35:131', 'HLA-B*35:132', 'HLA-B*35:133', 'HLA-B*35:135', 'HLA-B*35:136', 'HLA-B*35:137', 'HLA-B*35:138', 'HLA-B*35:139',
         'HLA-B*35:14', 'HLA-B*35:140', 'HLA-B*35:141', 'HLA-B*35:142', 'HLA-B*35:143', 'HLA-B*35:144', 'HLA-B*35:15', 'HLA-B*35:16', 'HLA-B*35:17',
         'HLA-B*35:18', 'HLA-B*35:19', 'HLA-B*35:20', 'HLA-B*35:21', 'HLA-B*35:22', 'HLA-B*35:23', 'HLA-B*35:24', 'HLA-B*35:25', 'HLA-B*35:26',
         'HLA-B*35:27', 'HLA-B*35:28', 'HLA-B*35:29', 'HLA-B*35:30', 'HLA-B*35:31', 'HLA-B*35:32', 'HLA-B*35:33', 'HLA-B*35:34', 'HLA-B*35:35',
         'HLA-B*35:36', 'HLA-B*35:37', 'HLA-B*35:38', 'HLA-B*35:39', 'HLA-B*35:41', 'HLA-B*35:42', 'HLA-B*35:43', 'HLA-B*35:44', 'HLA-B*35:45',
         'HLA-B*35:46', 'HLA-B*35:47', 'HLA-B*35:48', 'HLA-B*35:49', 'HLA-B*35:50', 'HLA-B*35:51', 'HLA-B*35:52', 'HLA-B*35:54', 'HLA-B*35:55',
         'HLA-B*35:56', 'HLA-B*35:57', 'HLA-B*35:58', 'HLA-B*35:59', 'HLA-B*35:60', 'HLA-B*35:61', 'HLA-B*35:62', 'HLA-B*35:63', 'HLA-B*35:64',
         'HLA-B*35:66', 'HLA-B*35:67', 'HLA-B*35:68', 'HLA-B*35:69', 'HLA-B*35:70', 'HLA-B*35:71', 'HLA-B*35:72', 'HLA-B*35:74', 'HLA-B*35:75',
         'HLA-B*35:76', 'HLA-B*35:77', 'HLA-B*35:78', 'HLA-B*35:79', 'HLA-B*35:80', 'HLA-B*35:81', 'HLA-B*35:82', 'HLA-B*35:83', 'HLA-B*35:84',
         'HLA-B*35:85', 'HLA-B*35:86', 'HLA-B*35:87', 'HLA-B*35:88', 'HLA-B*35:89', 'HLA-B*35:90', 'HLA-B*35:91', 'HLA-B*35:92', 'HLA-B*35:93',
         'HLA-B*35:94', 'HLA-B*35:95', 'HLA-B*35:96', 'HLA-B*35:97', 'HLA-B*35:98', 'HLA-B*35:99', 'HLA-B*37:01', 'HLA-B*37:02', 'HLA-B*37:04',
         'HLA-B*37:05', 'HLA-B*37:06', 'HLA-B*37:07', 'HLA-B*37:08', 'HLA-B*37:09', 'HLA-B*37:10', 'HLA-B*37:11', 'HLA-B*37:12', 'HLA-B*37:13',
         'HLA-B*37:14', 'HLA-B*37:15', 'HLA-B*37:17', 'HLA-B*37:18', 'HLA-B*37:19', 'HLA-B*37:20', 'HLA-B*37:21', 'HLA-B*37:22', 'HLA-B*37:23',
         'HLA-B*38:01', 'HLA-B*38:02', 'HLA-B*38:03', 'HLA-B*38:04', 'HLA-B*38:05', 'HLA-B*38:06', 'HLA-B*38:07', 'HLA-B*38:08', 'HLA-B*38:09',
         'HLA-B*38:10', 'HLA-B*38:11', 'HLA-B*38:12', 'HLA-B*38:13', 'HLA-B*38:14', 'HLA-B*38:15', 'HLA-B*38:16', 'HLA-B*38:17', 'HLA-B*38:18',
         'HLA-B*38:19', 'HLA-B*38:20', 'HLA-B*38:21', 'HLA-B*38:22', 'HLA-B*38:23', 'HLA-B*39:01', 'HLA-B*39:02', 'HLA-B*39:03', 'HLA-B*39:04',
         'HLA-B*39:05', 'HLA-B*39:06', 'HLA-B*39:07', 'HLA-B*39:08', 'HLA-B*39:09', 'HLA-B*39:10', 'HLA-B*39:11', 'HLA-B*39:12', 'HLA-B*39:13',
         'HLA-B*39:14', 'HLA-B*39:15', 'HLA-B*39:16', 'HLA-B*39:17', 'HLA-B*39:18', 'HLA-B*39:19', 'HLA-B*39:20', 'HLA-B*39:22', 'HLA-B*39:23',
         'HLA-B*39:24', 'HLA-B*39:26', 'HLA-B*39:27', 'HLA-B*39:28', 'HLA-B*39:29', 'HLA-B*39:30', 'HLA-B*39:31', 'HLA-B*39:32', 'HLA-B*39:33',
         'HLA-B*39:34', 'HLA-B*39:35', 'HLA-B*39:36', 'HLA-B*39:37', 'HLA-B*39:39', 'HLA-B*39:41', 'HLA-B*39:42', 'HLA-B*39:43', 'HLA-B*39:44',
         'HLA-B*39:45', 'HLA-B*39:46', 'HLA-B*39:47', 'HLA-B*39:48', 'HLA-B*39:49', 'HLA-B*39:50', 'HLA-B*39:51', 'HLA-B*39:52', 'HLA-B*39:53',
         'HLA-B*39:54', 'HLA-B*39:55', 'HLA-B*39:56', 'HLA-B*39:57', 'HLA-B*39:58', 'HLA-B*39:59', 'HLA-B*39:60', 'HLA-B*40:01', 'HLA-B*40:02',
         'HLA-B*40:03', 'HLA-B*40:04', 'HLA-B*40:05', 'HLA-B*40:06', 'HLA-B*40:07', 'HLA-B*40:08', 'HLA-B*40:09', 'HLA-B*40:10', 'HLA-B*40:100',
         'HLA-B*40:101', 'HLA-B*40:102', 'HLA-B*40:103', 'HLA-B*40:104', 'HLA-B*40:105', 'HLA-B*40:106', 'HLA-B*40:107', 'HLA-B*40:108',
         'HLA-B*40:109', 'HLA-B*40:11', 'HLA-B*40:110', 'HLA-B*40:111', 'HLA-B*40:112', 'HLA-B*40:113', 'HLA-B*40:114', 'HLA-B*40:115',
         'HLA-B*40:116', 'HLA-B*40:117', 'HLA-B*40:119', 'HLA-B*40:12', 'HLA-B*40:120', 'HLA-B*40:121', 'HLA-B*40:122', 'HLA-B*40:123',
         'HLA-B*40:124', 'HLA-B*40:125', 'HLA-B*40:126', 'HLA-B*40:127', 'HLA-B*40:128', 'HLA-B*40:129', 'HLA-B*40:13', 'HLA-B*40:130',
         'HLA-B*40:131', 'HLA-B*40:132', 'HLA-B*40:134', 'HLA-B*40:135', 'HLA-B*40:136', 'HLA-B*40:137', 'HLA-B*40:138', 'HLA-B*40:139',
         'HLA-B*40:14', 'HLA-B*40:140', 'HLA-B*40:141', 'HLA-B*40:143', 'HLA-B*40:145', 'HLA-B*40:146', 'HLA-B*40:147', 'HLA-B*40:15',
         'HLA-B*40:16', 'HLA-B*40:18', 'HLA-B*40:19', 'HLA-B*40:20', 'HLA-B*40:21', 'HLA-B*40:23', 'HLA-B*40:24', 'HLA-B*40:25', 'HLA-B*40:26',
         'HLA-B*40:27', 'HLA-B*40:28', 'HLA-B*40:29', 'HLA-B*40:30', 'HLA-B*40:31', 'HLA-B*40:32', 'HLA-B*40:33', 'HLA-B*40:34', 'HLA-B*40:35',
         'HLA-B*40:36', 'HLA-B*40:37', 'HLA-B*40:38', 'HLA-B*40:39', 'HLA-B*40:40', 'HLA-B*40:42', 'HLA-B*40:43', 'HLA-B*40:44', 'HLA-B*40:45',
         'HLA-B*40:46', 'HLA-B*40:47', 'HLA-B*40:48', 'HLA-B*40:49', 'HLA-B*40:50', 'HLA-B*40:51', 'HLA-B*40:52', 'HLA-B*40:53', 'HLA-B*40:54',
         'HLA-B*40:55', 'HLA-B*40:56', 'HLA-B*40:57', 'HLA-B*40:58', 'HLA-B*40:59', 'HLA-B*40:60', 'HLA-B*40:61', 'HLA-B*40:62', 'HLA-B*40:63',
         'HLA-B*40:64', 'HLA-B*40:65', 'HLA-B*40:66', 'HLA-B*40:67', 'HLA-B*40:68', 'HLA-B*40:69', 'HLA-B*40:70', 'HLA-B*40:71', 'HLA-B*40:72',
         'HLA-B*40:73', 'HLA-B*40:74', 'HLA-B*40:75', 'HLA-B*40:76', 'HLA-B*40:77', 'HLA-B*40:78', 'HLA-B*40:79', 'HLA-B*40:80', 'HLA-B*40:81',
         'HLA-B*40:82', 'HLA-B*40:83', 'HLA-B*40:84', 'HLA-B*40:85', 'HLA-B*40:86', 'HLA-B*40:87', 'HLA-B*40:88', 'HLA-B*40:89', 'HLA-B*40:90',
         'HLA-B*40:91', 'HLA-B*40:92', 'HLA-B*40:93', 'HLA-B*40:94', 'HLA-B*40:95', 'HLA-B*40:96', 'HLA-B*40:97', 'HLA-B*40:98', 'HLA-B*40:99',
         'HLA-B*41:01', 'HLA-B*41:02', 'HLA-B*41:03', 'HLA-B*41:04', 'HLA-B*41:05', 'HLA-B*41:06', 'HLA-B*41:07', 'HLA-B*41:08', 'HLA-B*41:09',
         'HLA-B*41:10', 'HLA-B*41:11', 'HLA-B*41:12', 'HLA-B*42:01', 'HLA-B*42:02', 'HLA-B*42:04', 'HLA-B*42:05', 'HLA-B*42:06', 'HLA-B*42:07',
         'HLA-B*42:08', 'HLA-B*42:09', 'HLA-B*42:10', 'HLA-B*42:11', 'HLA-B*42:12', 'HLA-B*42:13', 'HLA-B*42:14', 'HLA-B*44:02', 'HLA-B*44:03',
         'HLA-B*44:04', 'HLA-B*44:05', 'HLA-B*44:06', 'HLA-B*44:07', 'HLA-B*44:08', 'HLA-B*44:09', 'HLA-B*44:10', 'HLA-B*44:100', 'HLA-B*44:101',
         'HLA-B*44:102', 'HLA-B*44:103', 'HLA-B*44:104', 'HLA-B*44:105', 'HLA-B*44:106', 'HLA-B*44:107', 'HLA-B*44:109', 'HLA-B*44:11',
         'HLA-B*44:110', 'HLA-B*44:12', 'HLA-B*44:13', 'HLA-B*44:14', 'HLA-B*44:15', 'HLA-B*44:16', 'HLA-B*44:17', 'HLA-B*44:18', 'HLA-B*44:20',
         'HLA-B*44:21', 'HLA-B*44:22', 'HLA-B*44:24', 'HLA-B*44:25', 'HLA-B*44:26', 'HLA-B*44:27', 'HLA-B*44:28', 'HLA-B*44:29', 'HLA-B*44:30',
         'HLA-B*44:31', 'HLA-B*44:32', 'HLA-B*44:33', 'HLA-B*44:34', 'HLA-B*44:35', 'HLA-B*44:36', 'HLA-B*44:37', 'HLA-B*44:38', 'HLA-B*44:39',
         'HLA-B*44:40', 'HLA-B*44:41', 'HLA-B*44:42', 'HLA-B*44:43', 'HLA-B*44:44', 'HLA-B*44:45', 'HLA-B*44:46', 'HLA-B*44:47', 'HLA-B*44:48',
         'HLA-B*44:49', 'HLA-B*44:50', 'HLA-B*44:51', 'HLA-B*44:53', 'HLA-B*44:54', 'HLA-B*44:55', 'HLA-B*44:57', 'HLA-B*44:59', 'HLA-B*44:60',
         'HLA-B*44:62', 'HLA-B*44:63', 'HLA-B*44:64', 'HLA-B*44:65', 'HLA-B*44:66', 'HLA-B*44:67', 'HLA-B*44:68', 'HLA-B*44:69', 'HLA-B*44:70',
         'HLA-B*44:71', 'HLA-B*44:72', 'HLA-B*44:73', 'HLA-B*44:74', 'HLA-B*44:75', 'HLA-B*44:76', 'HLA-B*44:77', 'HLA-B*44:78', 'HLA-B*44:79',
         'HLA-B*44:80', 'HLA-B*44:81', 'HLA-B*44:82', 'HLA-B*44:83', 'HLA-B*44:84', 'HLA-B*44:85', 'HLA-B*44:86', 'HLA-B*44:87', 'HLA-B*44:88',
         'HLA-B*44:89', 'HLA-B*44:90', 'HLA-B*44:91', 'HLA-B*44:92', 'HLA-B*44:93', 'HLA-B*44:94', 'HLA-B*44:95', 'HLA-B*44:96', 'HLA-B*44:97',
         'HLA-B*44:98', 'HLA-B*44:99', 'HLA-B*45:01', 'HLA-B*45:02', 'HLA-B*45:03', 'HLA-B*45:04', 'HLA-B*45:05', 'HLA-B*45:06', 'HLA-B*45:07',
         'HLA-B*45:08', 'HLA-B*45:09', 'HLA-B*45:10', 'HLA-B*45:11', 'HLA-B*45:12', 'HLA-B*46:01', 'HLA-B*46:02', 'HLA-B*46:03', 'HLA-B*46:04',
         'HLA-B*46:05', 'HLA-B*46:06', 'HLA-B*46:08', 'HLA-B*46:09', 'HLA-B*46:10', 'HLA-B*46:11', 'HLA-B*46:12', 'HLA-B*46:13', 'HLA-B*46:14',
         'HLA-B*46:16', 'HLA-B*46:17', 'HLA-B*46:18', 'HLA-B*46:19', 'HLA-B*46:20', 'HLA-B*46:21', 'HLA-B*46:22', 'HLA-B*46:23', 'HLA-B*46:24',
         'HLA-B*47:01', 'HLA-B*47:02', 'HLA-B*47:03', 'HLA-B*47:04', 'HLA-B*47:05', 'HLA-B*47:06', 'HLA-B*47:07', 'HLA-B*48:01', 'HLA-B*48:02',
         'HLA-B*48:03', 'HLA-B*48:04', 'HLA-B*48:05', 'HLA-B*48:06', 'HLA-B*48:07', 'HLA-B*48:08', 'HLA-B*48:09', 'HLA-B*48:10', 'HLA-B*48:11',
         'HLA-B*48:12', 'HLA-B*48:13', 'HLA-B*48:14', 'HLA-B*48:15', 'HLA-B*48:16', 'HLA-B*48:17', 'HLA-B*48:18', 'HLA-B*48:19', 'HLA-B*48:20',
         'HLA-B*48:21', 'HLA-B*48:22', 'HLA-B*48:23', 'HLA-B*49:01', 'HLA-B*49:02', 'HLA-B*49:03', 'HLA-B*49:04', 'HLA-B*49:05', 'HLA-B*49:06',
         'HLA-B*49:07', 'HLA-B*49:08', 'HLA-B*49:09', 'HLA-B*49:10', 'HLA-B*50:01', 'HLA-B*50:02', 'HLA-B*50:04', 'HLA-B*50:05', 'HLA-B*50:06',
         'HLA-B*50:07', 'HLA-B*50:08', 'HLA-B*50:09', 'HLA-B*51:01', 'HLA-B*51:02', 'HLA-B*51:03', 'HLA-B*51:04', 'HLA-B*51:05', 'HLA-B*51:06',
         'HLA-B*51:07', 'HLA-B*51:08', 'HLA-B*51:09', 'HLA-B*51:12', 'HLA-B*51:13', 'HLA-B*51:14', 'HLA-B*51:15', 'HLA-B*51:16', 'HLA-B*51:17',
         'HLA-B*51:18', 'HLA-B*51:19', 'HLA-B*51:20', 'HLA-B*51:21', 'HLA-B*51:22', 'HLA-B*51:23', 'HLA-B*51:24', 'HLA-B*51:26', 'HLA-B*51:28',
         'HLA-B*51:29', 'HLA-B*51:30', 'HLA-B*51:31', 'HLA-B*51:32', 'HLA-B*51:33', 'HLA-B*51:34', 'HLA-B*51:35', 'HLA-B*51:36', 'HLA-B*51:37',
         'HLA-B*51:38', 'HLA-B*51:39', 'HLA-B*51:40', 'HLA-B*51:42', 'HLA-B*51:43', 'HLA-B*51:45', 'HLA-B*51:46', 'HLA-B*51:48', 'HLA-B*51:49',
         'HLA-B*51:50', 'HLA-B*51:51', 'HLA-B*51:52', 'HLA-B*51:53', 'HLA-B*51:54', 'HLA-B*51:55', 'HLA-B*51:56', 'HLA-B*51:57', 'HLA-B*51:58',
         'HLA-B*51:59', 'HLA-B*51:60', 'HLA-B*51:61', 'HLA-B*51:62', 'HLA-B*51:63', 'HLA-B*51:64', 'HLA-B*51:65', 'HLA-B*51:66', 'HLA-B*51:67',
         'HLA-B*51:68', 'HLA-B*51:69', 'HLA-B*51:70', 'HLA-B*51:71', 'HLA-B*51:72', 'HLA-B*51:73', 'HLA-B*51:74', 'HLA-B*51:75', 'HLA-B*51:76',
         'HLA-B*51:77', 'HLA-B*51:78', 'HLA-B*51:79', 'HLA-B*51:80', 'HLA-B*51:81', 'HLA-B*51:82', 'HLA-B*51:83', 'HLA-B*51:84', 'HLA-B*51:85',
         'HLA-B*51:86', 'HLA-B*51:87', 'HLA-B*51:88', 'HLA-B*51:89', 'HLA-B*51:90', 'HLA-B*51:91', 'HLA-B*51:92', 'HLA-B*51:93', 'HLA-B*51:94',
         'HLA-B*51:95', 'HLA-B*51:96', 'HLA-B*52:01', 'HLA-B*52:02', 'HLA-B*52:03', 'HLA-B*52:04', 'HLA-B*52:05', 'HLA-B*52:06', 'HLA-B*52:07',
         'HLA-B*52:08', 'HLA-B*52:09', 'HLA-B*52:10', 'HLA-B*52:11', 'HLA-B*52:12', 'HLA-B*52:13', 'HLA-B*52:14', 'HLA-B*52:15', 'HLA-B*52:16',
         'HLA-B*52:17', 'HLA-B*52:18', 'HLA-B*52:19', 'HLA-B*52:20', 'HLA-B*52:21', 'HLA-B*53:01', 'HLA-B*53:02', 'HLA-B*53:03', 'HLA-B*53:04',
         'HLA-B*53:05', 'HLA-B*53:06', 'HLA-B*53:07', 'HLA-B*53:08', 'HLA-B*53:09', 'HLA-B*53:10', 'HLA-B*53:11', 'HLA-B*53:12', 'HLA-B*53:13',
         'HLA-B*53:14', 'HLA-B*53:15', 'HLA-B*53:16', 'HLA-B*53:17', 'HLA-B*53:18', 'HLA-B*53:19', 'HLA-B*53:20', 'HLA-B*53:21', 'HLA-B*53:22',
         'HLA-B*53:23', 'HLA-B*54:01', 'HLA-B*54:02', 'HLA-B*54:03', 'HLA-B*54:04', 'HLA-B*54:06', 'HLA-B*54:07', 'HLA-B*54:09', 'HLA-B*54:10',
         'HLA-B*54:11', 'HLA-B*54:12', 'HLA-B*54:13', 'HLA-B*54:14', 'HLA-B*54:15', 'HLA-B*54:16', 'HLA-B*54:17', 'HLA-B*54:18', 'HLA-B*54:19',
         'HLA-B*54:20', 'HLA-B*54:21', 'HLA-B*54:22', 'HLA-B*54:23', 'HLA-B*55:01', 'HLA-B*55:02', 'HLA-B*55:03', 'HLA-B*55:04', 'HLA-B*55:05',
         'HLA-B*55:07', 'HLA-B*55:08', 'HLA-B*55:09', 'HLA-B*55:10', 'HLA-B*55:11', 'HLA-B*55:12', 'HLA-B*55:13', 'HLA-B*55:14', 'HLA-B*55:15',
         'HLA-B*55:16', 'HLA-B*55:17', 'HLA-B*55:18', 'HLA-B*55:19', 'HLA-B*55:20', 'HLA-B*55:21', 'HLA-B*55:22', 'HLA-B*55:23', 'HLA-B*55:24',
         'HLA-B*55:25', 'HLA-B*55:26', 'HLA-B*55:27', 'HLA-B*55:28', 'HLA-B*55:29', 'HLA-B*55:30', 'HLA-B*55:31', 'HLA-B*55:32', 'HLA-B*55:33',
         'HLA-B*55:34', 'HLA-B*55:35', 'HLA-B*55:36', 'HLA-B*55:37', 'HLA-B*55:38', 'HLA-B*55:39', 'HLA-B*55:40', 'HLA-B*55:41', 'HLA-B*55:42',
         'HLA-B*55:43', 'HLA-B*56:01', 'HLA-B*56:02', 'HLA-B*56:03', 'HLA-B*56:04', 'HLA-B*56:05', 'HLA-B*56:06', 'HLA-B*56:07', 'HLA-B*56:08',
         'HLA-B*56:09', 'HLA-B*56:10', 'HLA-B*56:11', 'HLA-B*56:12', 'HLA-B*56:13', 'HLA-B*56:14', 'HLA-B*56:15', 'HLA-B*56:16', 'HLA-B*56:17',
         'HLA-B*56:18', 'HLA-B*56:20', 'HLA-B*56:21', 'HLA-B*56:22', 'HLA-B*56:23', 'HLA-B*56:24', 'HLA-B*56:25', 'HLA-B*56:26', 'HLA-B*56:27',
         'HLA-B*56:29', 'HLA-B*57:01', 'HLA-B*57:02', 'HLA-B*57:03', 'HLA-B*57:04', 'HLA-B*57:05', 'HLA-B*57:06', 'HLA-B*57:07', 'HLA-B*57:08',
         'HLA-B*57:09', 'HLA-B*57:10', 'HLA-B*57:11', 'HLA-B*57:12', 'HLA-B*57:13', 'HLA-B*57:14', 'HLA-B*57:15', 'HLA-B*57:16', 'HLA-B*57:17',
         'HLA-B*57:18', 'HLA-B*57:19', 'HLA-B*57:20', 'HLA-B*57:21', 'HLA-B*57:22', 'HLA-B*57:23', 'HLA-B*57:24', 'HLA-B*57:25', 'HLA-B*57:26',
         'HLA-B*57:27', 'HLA-B*57:29', 'HLA-B*57:30', 'HLA-B*57:31', 'HLA-B*57:32', 'HLA-B*58:01', 'HLA-B*58:02', 'HLA-B*58:04', 'HLA-B*58:05',
         'HLA-B*58:06', 'HLA-B*58:07', 'HLA-B*58:08', 'HLA-B*58:09', 'HLA-B*58:11', 'HLA-B*58:12', 'HLA-B*58:13', 'HLA-B*58:14', 'HLA-B*58:15',
         'HLA-B*58:16', 'HLA-B*58:18', 'HLA-B*58:19', 'HLA-B*58:20', 'HLA-B*58:21', 'HLA-B*58:22', 'HLA-B*58:23', 'HLA-B*58:24', 'HLA-B*58:25',
         'HLA-B*58:26', 'HLA-B*58:27', 'HLA-B*58:28', 'HLA-B*58:29', 'HLA-B*58:30', 'HLA-B*59:01', 'HLA-B*59:02', 'HLA-B*59:03', 'HLA-B*59:04',
         'HLA-B*59:05', 'HLA-B*67:01', 'HLA-B*67:02', 'HLA-B*73:01', 'HLA-B*73:02', 'HLA-B*78:01', 'HLA-B*78:02', 'HLA-B*78:03', 'HLA-B*78:04',
         'HLA-B*78:05', 'HLA-B*78:06', 'HLA-B*78:07', 'HLA-B*81:01', 'HLA-B*81:02', 'HLA-B*81:03', 'HLA-B*81:05', 'HLA-B*82:01', 'HLA-B*82:02',
         'HLA-B*82:03', 'HLA-B*83:01', 'HLA-C*01:02', 'HLA-C*01:03', 'HLA-C*01:04', 'HLA-C*01:05', 'HLA-C*01:06', 'HLA-C*01:07', 'HLA-C*01:08',
         'HLA-C*01:09', 'HLA-C*01:10', 'HLA-C*01:11', 'HLA-C*01:12', 'HLA-C*01:13', 'HLA-C*01:14', 'HLA-C*01:15', 'HLA-C*01:16', 'HLA-C*01:17',
         'HLA-C*01:18', 'HLA-C*01:19', 'HLA-C*01:20', 'HLA-C*01:21', 'HLA-C*01:22', 'HLA-C*01:23', 'HLA-C*01:24', 'HLA-C*01:25', 'HLA-C*01:26',
         'HLA-C*01:27', 'HLA-C*01:28', 'HLA-C*01:29', 'HLA-C*01:30', 'HLA-C*01:31', 'HLA-C*01:32', 'HLA-C*01:33', 'HLA-C*01:34', 'HLA-C*01:35',
         'HLA-C*01:36', 'HLA-C*01:38', 'HLA-C*01:39', 'HLA-C*01:40', 'HLA-C*02:02', 'HLA-C*02:03', 'HLA-C*02:04', 'HLA-C*02:05', 'HLA-C*02:06',
         'HLA-C*02:07', 'HLA-C*02:08', 'HLA-C*02:09', 'HLA-C*02:10', 'HLA-C*02:11', 'HLA-C*02:12', 'HLA-C*02:13', 'HLA-C*02:14', 'HLA-C*02:15',
         'HLA-C*02:16', 'HLA-C*02:17', 'HLA-C*02:18', 'HLA-C*02:19', 'HLA-C*02:20', 'HLA-C*02:21', 'HLA-C*02:22', 'HLA-C*02:23', 'HLA-C*02:24',
         'HLA-C*02:26', 'HLA-C*02:27', 'HLA-C*02:28', 'HLA-C*02:29', 'HLA-C*02:30', 'HLA-C*02:31', 'HLA-C*02:32', 'HLA-C*02:33', 'HLA-C*02:34',
         'HLA-C*02:35', 'HLA-C*02:36', 'HLA-C*02:37', 'HLA-C*02:39', 'HLA-C*02:40', 'HLA-C*03:01', 'HLA-C*03:02', 'HLA-C*03:03', 'HLA-C*03:04',
         'HLA-C*03:05', 'HLA-C*03:06', 'HLA-C*03:07', 'HLA-C*03:08', 'HLA-C*03:09', 'HLA-C*03:10', 'HLA-C*03:11', 'HLA-C*03:12', 'HLA-C*03:13',
         'HLA-C*03:14', 'HLA-C*03:15', 'HLA-C*03:16', 'HLA-C*03:17', 'HLA-C*03:18', 'HLA-C*03:19', 'HLA-C*03:21', 'HLA-C*03:23', 'HLA-C*03:24',
         'HLA-C*03:25', 'HLA-C*03:26', 'HLA-C*03:27', 'HLA-C*03:28', 'HLA-C*03:29', 'HLA-C*03:30', 'HLA-C*03:31', 'HLA-C*03:32', 'HLA-C*03:33',
         'HLA-C*03:34', 'HLA-C*03:35', 'HLA-C*03:36', 'HLA-C*03:37', 'HLA-C*03:38', 'HLA-C*03:39', 'HLA-C*03:40', 'HLA-C*03:41', 'HLA-C*03:42',
         'HLA-C*03:43', 'HLA-C*03:44', 'HLA-C*03:45', 'HLA-C*03:46', 'HLA-C*03:47', 'HLA-C*03:48', 'HLA-C*03:49', 'HLA-C*03:50', 'HLA-C*03:51',
         'HLA-C*03:52', 'HLA-C*03:53', 'HLA-C*03:54', 'HLA-C*03:55', 'HLA-C*03:56', 'HLA-C*03:57', 'HLA-C*03:58', 'HLA-C*03:59', 'HLA-C*03:60',
         'HLA-C*03:61', 'HLA-C*03:62', 'HLA-C*03:63', 'HLA-C*03:64', 'HLA-C*03:65', 'HLA-C*03:66', 'HLA-C*03:67', 'HLA-C*03:68', 'HLA-C*03:69',
         'HLA-C*03:70', 'HLA-C*03:71', 'HLA-C*03:72', 'HLA-C*03:73', 'HLA-C*03:74', 'HLA-C*03:75', 'HLA-C*03:76', 'HLA-C*03:77', 'HLA-C*03:78',
         'HLA-C*03:79', 'HLA-C*03:80', 'HLA-C*03:81', 'HLA-C*03:82', 'HLA-C*03:83', 'HLA-C*03:84', 'HLA-C*03:85', 'HLA-C*03:86', 'HLA-C*03:87',
         'HLA-C*03:88', 'HLA-C*03:89', 'HLA-C*03:90', 'HLA-C*03:91', 'HLA-C*03:92', 'HLA-C*03:93', 'HLA-C*03:94', 'HLA-C*04:01', 'HLA-C*04:03',
         'HLA-C*04:04', 'HLA-C*04:05', 'HLA-C*04:06', 'HLA-C*04:07', 'HLA-C*04:08', 'HLA-C*04:10', 'HLA-C*04:11', 'HLA-C*04:12', 'HLA-C*04:13',
         'HLA-C*04:14', 'HLA-C*04:15', 'HLA-C*04:16', 'HLA-C*04:17', 'HLA-C*04:18', 'HLA-C*04:19', 'HLA-C*04:20', 'HLA-C*04:23', 'HLA-C*04:24',
         'HLA-C*04:25', 'HLA-C*04:26', 'HLA-C*04:27', 'HLA-C*04:28', 'HLA-C*04:29', 'HLA-C*04:30', 'HLA-C*04:31', 'HLA-C*04:32', 'HLA-C*04:33',
         'HLA-C*04:34', 'HLA-C*04:35', 'HLA-C*04:36', 'HLA-C*04:37', 'HLA-C*04:38', 'HLA-C*04:39', 'HLA-C*04:40', 'HLA-C*04:41', 'HLA-C*04:42',
         'HLA-C*04:43', 'HLA-C*04:44', 'HLA-C*04:45', 'HLA-C*04:46', 'HLA-C*04:47', 'HLA-C*04:48', 'HLA-C*04:49', 'HLA-C*04:50', 'HLA-C*04:51',
         'HLA-C*04:52', 'HLA-C*04:53', 'HLA-C*04:54', 'HLA-C*04:55', 'HLA-C*04:56', 'HLA-C*04:57', 'HLA-C*04:58', 'HLA-C*04:60', 'HLA-C*04:61',
         'HLA-C*04:62', 'HLA-C*04:63', 'HLA-C*04:64', 'HLA-C*04:65', 'HLA-C*04:66', 'HLA-C*04:67', 'HLA-C*04:68', 'HLA-C*04:69', 'HLA-C*04:70',
         'HLA-C*05:01', 'HLA-C*05:03', 'HLA-C*05:04', 'HLA-C*05:05', 'HLA-C*05:06', 'HLA-C*05:08', 'HLA-C*05:09', 'HLA-C*05:10', 'HLA-C*05:11',
         'HLA-C*05:12', 'HLA-C*05:13', 'HLA-C*05:14', 'HLA-C*05:15', 'HLA-C*05:16', 'HLA-C*05:17', 'HLA-C*05:18', 'HLA-C*05:19', 'HLA-C*05:20',
         'HLA-C*05:21', 'HLA-C*05:22', 'HLA-C*05:23', 'HLA-C*05:24', 'HLA-C*05:25', 'HLA-C*05:26', 'HLA-C*05:27', 'HLA-C*05:28', 'HLA-C*05:29',
         'HLA-C*05:30', 'HLA-C*05:31', 'HLA-C*05:32', 'HLA-C*05:33', 'HLA-C*05:34', 'HLA-C*05:35', 'HLA-C*05:36', 'HLA-C*05:37', 'HLA-C*05:38',
         'HLA-C*05:39', 'HLA-C*05:40', 'HLA-C*05:41', 'HLA-C*05:42', 'HLA-C*05:43', 'HLA-C*05:44', 'HLA-C*05:45', 'HLA-C*06:02', 'HLA-C*06:03',
         'HLA-C*06:04', 'HLA-C*06:05', 'HLA-C*06:06', 'HLA-C*06:07', 'HLA-C*06:08', 'HLA-C*06:09', 'HLA-C*06:10', 'HLA-C*06:11', 'HLA-C*06:12',
         'HLA-C*06:13', 'HLA-C*06:14', 'HLA-C*06:15', 'HLA-C*06:17', 'HLA-C*06:18', 'HLA-C*06:19', 'HLA-C*06:20', 'HLA-C*06:21', 'HLA-C*06:22',
         'HLA-C*06:23', 'HLA-C*06:24', 'HLA-C*06:25', 'HLA-C*06:26', 'HLA-C*06:27', 'HLA-C*06:28', 'HLA-C*06:29', 'HLA-C*06:30', 'HLA-C*06:31',
         'HLA-C*06:32', 'HLA-C*06:33', 'HLA-C*06:34', 'HLA-C*06:35', 'HLA-C*06:36', 'HLA-C*06:37', 'HLA-C*06:38', 'HLA-C*06:39', 'HLA-C*06:40',
         'HLA-C*06:41', 'HLA-C*06:42', 'HLA-C*06:43', 'HLA-C*06:44', 'HLA-C*06:45', 'HLA-C*07:01', 'HLA-C*07:02', 'HLA-C*07:03', 'HLA-C*07:04',
         'HLA-C*07:05', 'HLA-C*07:06', 'HLA-C*07:07', 'HLA-C*07:08', 'HLA-C*07:09', 'HLA-C*07:10', 'HLA-C*07:100', 'HLA-C*07:101', 'HLA-C*07:102',
         'HLA-C*07:103', 'HLA-C*07:105', 'HLA-C*07:106', 'HLA-C*07:107', 'HLA-C*07:108', 'HLA-C*07:109', 'HLA-C*07:11', 'HLA-C*07:110',
         'HLA-C*07:111', 'HLA-C*07:112', 'HLA-C*07:113', 'HLA-C*07:114', 'HLA-C*07:115', 'HLA-C*07:116', 'HLA-C*07:117', 'HLA-C*07:118',
         'HLA-C*07:119', 'HLA-C*07:12', 'HLA-C*07:120', 'HLA-C*07:122', 'HLA-C*07:123', 'HLA-C*07:124', 'HLA-C*07:125', 'HLA-C*07:126',
         'HLA-C*07:127', 'HLA-C*07:128', 'HLA-C*07:129', 'HLA-C*07:13', 'HLA-C*07:130', 'HLA-C*07:131', 'HLA-C*07:132', 'HLA-C*07:133',
         'HLA-C*07:134', 'HLA-C*07:135', 'HLA-C*07:136', 'HLA-C*07:137', 'HLA-C*07:138', 'HLA-C*07:139', 'HLA-C*07:14', 'HLA-C*07:140',
         'HLA-C*07:141', 'HLA-C*07:142', 'HLA-C*07:143', 'HLA-C*07:144', 'HLA-C*07:145', 'HLA-C*07:146', 'HLA-C*07:147', 'HLA-C*07:148',
         'HLA-C*07:149', 'HLA-C*07:15', 'HLA-C*07:16', 'HLA-C*07:17', 'HLA-C*07:18', 'HLA-C*07:19', 'HLA-C*07:20', 'HLA-C*07:21', 'HLA-C*07:22',
         'HLA-C*07:23', 'HLA-C*07:24', 'HLA-C*07:25', 'HLA-C*07:26', 'HLA-C*07:27', 'HLA-C*07:28', 'HLA-C*07:29', 'HLA-C*07:30', 'HLA-C*07:31',
         'HLA-C*07:35', 'HLA-C*07:36', 'HLA-C*07:37', 'HLA-C*07:38', 'HLA-C*07:39', 'HLA-C*07:40', 'HLA-C*07:41', 'HLA-C*07:42', 'HLA-C*07:43',
         'HLA-C*07:44', 'HLA-C*07:45', 'HLA-C*07:46', 'HLA-C*07:47', 'HLA-C*07:48', 'HLA-C*07:49', 'HLA-C*07:50', 'HLA-C*07:51', 'HLA-C*07:52',
         'HLA-C*07:53', 'HLA-C*07:54', 'HLA-C*07:56', 'HLA-C*07:57', 'HLA-C*07:58', 'HLA-C*07:59', 'HLA-C*07:60', 'HLA-C*07:62', 'HLA-C*07:63',
         'HLA-C*07:64', 'HLA-C*07:65', 'HLA-C*07:66', 'HLA-C*07:67', 'HLA-C*07:68', 'HLA-C*07:69', 'HLA-C*07:70', 'HLA-C*07:71', 'HLA-C*07:72',
         'HLA-C*07:73', 'HLA-C*07:74', 'HLA-C*07:75', 'HLA-C*07:76', 'HLA-C*07:77', 'HLA-C*07:78', 'HLA-C*07:79', 'HLA-C*07:80', 'HLA-C*07:81',
         'HLA-C*07:82', 'HLA-C*07:83', 'HLA-C*07:84', 'HLA-C*07:85', 'HLA-C*07:86', 'HLA-C*07:87', 'HLA-C*07:88', 'HLA-C*07:89', 'HLA-C*07:90',
         'HLA-C*07:91', 'HLA-C*07:92', 'HLA-C*07:93', 'HLA-C*07:94', 'HLA-C*07:95', 'HLA-C*07:96', 'HLA-C*07:97', 'HLA-C*07:99', 'HLA-C*08:01',
         'HLA-C*08:02', 'HLA-C*08:03', 'HLA-C*08:04', 'HLA-C*08:05', 'HLA-C*08:06', 'HLA-C*08:07', 'HLA-C*08:08', 'HLA-C*08:09', 'HLA-C*08:10',
         'HLA-C*08:11', 'HLA-C*08:12', 'HLA-C*08:13', 'HLA-C*08:14', 'HLA-C*08:15', 'HLA-C*08:16', 'HLA-C*08:17', 'HLA-C*08:18', 'HLA-C*08:19',
         'HLA-C*08:20', 'HLA-C*08:21', 'HLA-C*08:22', 'HLA-C*08:23', 'HLA-C*08:24', 'HLA-C*08:25', 'HLA-C*08:27', 'HLA-C*08:28', 'HLA-C*08:29',
         'HLA-C*08:30', 'HLA-C*08:31', 'HLA-C*08:32', 'HLA-C*08:33', 'HLA-C*08:34', 'HLA-C*08:35', 'HLA-C*12:02', 'HLA-C*12:03', 'HLA-C*12:04',
         'HLA-C*12:05', 'HLA-C*12:06', 'HLA-C*12:07', 'HLA-C*12:08', 'HLA-C*12:09', 'HLA-C*12:10', 'HLA-C*12:11', 'HLA-C*12:12', 'HLA-C*12:13',
         'HLA-C*12:14', 'HLA-C*12:15', 'HLA-C*12:16', 'HLA-C*12:17', 'HLA-C*12:18', 'HLA-C*12:19', 'HLA-C*12:20', 'HLA-C*12:21', 'HLA-C*12:22',
         'HLA-C*12:23', 'HLA-C*12:24', 'HLA-C*12:25', 'HLA-C*12:26', 'HLA-C*12:27', 'HLA-C*12:28', 'HLA-C*12:29', 'HLA-C*12:30', 'HLA-C*12:31',
         'HLA-C*12:32', 'HLA-C*12:33', 'HLA-C*12:34', 'HLA-C*12:35', 'HLA-C*12:36', 'HLA-C*12:37', 'HLA-C*12:38', 'HLA-C*12:40', 'HLA-C*12:41',
         'HLA-C*12:43', 'HLA-C*12:44', 'HLA-C*14:02', 'HLA-C*14:03', 'HLA-C*14:04', 'HLA-C*14:05', 'HLA-C*14:06', 'HLA-C*14:08', 'HLA-C*14:09',
         'HLA-C*14:10', 'HLA-C*14:11', 'HLA-C*14:12', 'HLA-C*14:13', 'HLA-C*14:14', 'HLA-C*14:15', 'HLA-C*14:16', 'HLA-C*14:17', 'HLA-C*14:18',
         'HLA-C*14:19', 'HLA-C*14:20', 'HLA-C*15:02', 'HLA-C*15:03', 'HLA-C*15:04', 'HLA-C*15:05', 'HLA-C*15:06', 'HLA-C*15:07', 'HLA-C*15:08',
         'HLA-C*15:09', 'HLA-C*15:10', 'HLA-C*15:11', 'HLA-C*15:12', 'HLA-C*15:13', 'HLA-C*15:15', 'HLA-C*15:16', 'HLA-C*15:17', 'HLA-C*15:18',
         'HLA-C*15:19', 'HLA-C*15:20', 'HLA-C*15:21', 'HLA-C*15:22', 'HLA-C*15:23', 'HLA-C*15:24', 'HLA-C*15:25', 'HLA-C*15:26', 'HLA-C*15:27',
         'HLA-C*15:28', 'HLA-C*15:29', 'HLA-C*15:30', 'HLA-C*15:31', 'HLA-C*15:33', 'HLA-C*15:34', 'HLA-C*15:35', 'HLA-C*16:01', 'HLA-C*16:02',
         'HLA-C*16:04', 'HLA-C*16:06', 'HLA-C*16:07', 'HLA-C*16:08', 'HLA-C*16:09', 'HLA-C*16:10', 'HLA-C*16:11', 'HLA-C*16:12', 'HLA-C*16:13',
         'HLA-C*16:14', 'HLA-C*16:15', 'HLA-C*16:17', 'HLA-C*16:18', 'HLA-C*16:19', 'HLA-C*16:20', 'HLA-C*16:21', 'HLA-C*16:22', 'HLA-C*16:23',
         'HLA-C*16:24', 'HLA-C*16:25', 'HLA-C*16:26', 'HLA-C*17:01', 'HLA-C*17:02', 'HLA-C*17:03', 'HLA-C*17:04', 'HLA-C*17:05', 'HLA-C*17:06',
         'HLA-C*17:07', 'HLA-C*18:01', 'HLA-C*18:02', 'HLA-C*18:03', 'HLA-E*01:01', 'HLA-G*01:01', 'HLA-G*01:02', 'HLA-G*01:03', 'HLA-G*01:04',
         'HLA-G*01:06', 'HLA-G*01:07', 'HLA-G*01:08', 'HLA-G*01:09',
         'H-2-Db', 'H-2-Dd', 'H-2-Kb', 'H-2-Kd', 'H-2-Kk', 'H-2-Ld', "H-2-Qa1", "H-2-Qa2"])

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s:%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools
        and writes them to file in the specific format

        No return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter = '\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in next(f) if x != ""]
        next(f)
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCPAN_2_8]
            for i, a in enumerate(alleles):
                if row[ScoreIndex.NETMHCPAN_2_8 + i * Offset.NETMHCPAN_2_8] != "1-log50k":     # Avoid header column, only access raw and rank scores
                    scores[a][pep_seq] = float(row[ScoreIndex.NETMHCPAN_2_8 + i * Offset.NETMHCPAN_2_8])
                    ranks[a][pep_seq] = float(row[RankIndex.NETMHCPAN_2_8 + i * Offset.NETMHCPAN_2_8])
        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
        return result


class NetMHCpan_3_0(NetMHCpan_2_8):
    """
        Implements the NetMHC binding version 3.0
        Supported  MHC alleles currently only restricted to HLA alleles.

    .. note::

        Nielsen, M., & Andreatta, M. (2016).
        NetMHCpan-3.0; improved prediction of binding to MHC class I molecules integrating information from multiple
        receptor and peptide length datasets. Genome Medicine, 8(1), 1.
    """

    __version = "3.0"
    __command = "netMHCpan -p {peptides} -a {alleles} {options} -xls -xlsfile {out}"


    @property
    def version(self):
        return self.__version

    @property
    def command(self):
        return self.__command

    def parse_external_result(self, file):
        """
        Parses external results and returns the result

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter = '\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in next(f) if x != ""]
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCPAN_3_0]
            for i, a in enumerate(alleles):
                if row[ScoreIndex.NETMHCPAN_3_0 + i * Offset.NETMHCPAN_3_0] != "1-log50k":     # Avoid header column, only access raw and rank scores
                    scores[a][pep_seq] = float(row[ScoreIndex.NETMHCPAN_3_0 + i * Offset.NETMHCPAN_3_0])
                    ranks[a][pep_seq] = float(row[RankIndex.NETMHCPAN_3_0 + i * Offset.NETMHCPAN_3_0])
                    
        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
        return result


class NetMHCpan_4_0(NetMHCpan_3_0):
    """
        Implements the NetMHC binding version 4.0
        Supported  MHC alleles currently only restricted to HLA alleles.
    """
    __version = "4.0"
    __command = "netMHCpan -p {peptides} -a {alleles} {options} -xls -xlsfile {out}"
    @property
    def version(self):
        return self.__version

    @property
    def command(self):
        return self.__command

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter = '\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in next(f) if x != ""]
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCPAN_4_0]
            for i, a in enumerate(alleles):
                if row[ScoreIndex.NETMHCPAN_4_0 + i * Offset.NETMHCPAN_4_0] != "1-log50k":     # Avoid header column, only access raw and rank scores
                    scores[a][pep_seq] = float(row[ScoreIndex.NETMHCPAN_4_0 + i * Offset.NETMHCPAN_4_0])
                    ranks[a][pep_seq] = float(row[RankIndex.NETMHCPAN_4_0 + i * Offset.NETMHCPAN_4_0])
        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
        return result



class NetMHCpan_4_1(NetMHCpan_4_0):
    """
        Implements the NetMHC binding version 4.1
        Supported  MHC alleles currently only restricted to HLA alleles.
    """
    __version = "4.1"
    __command = "netMHCpan -p {peptides} -a {alleles} {options} -xls -xlsfile {out}"
    @property
    def version(self):
        return self.__version

    @property
    def command(self):
        return self.__command

    __alleles = frozenset(['H-2-Db','H-2-Dd','H-2-Dq','H-2-Kb','H-2-Kd','H-2-Kk','H-2-Kq','H-2-Ld','H-2-Lq','H-2-Qa1','H-2-Qa2','HLA-A*01:01','HLA-A*01:02','HLA-A*01:03','HLA-A*01:04','HLA-A*01:06','HLA-A*01:07','HLA-A*01:08','HLA-A*01:09','HLA-A*01:10','HLA-A*01:100','HLA-A*01:101','HLA-A*01:102','HLA-A*01:103','HLA-A*01:104','HLA-A*01:105','HLA-A*01:106','HLA-A*01:107','HLA-A*01:108','HLA-A*01:109','HLA-A*01:110','HLA-A*01:111','HLA-A*01:112','HLA-A*01:113','HLA-A*01:114','HLA-A*01:115','HLA-A*01:116','HLA-A*01:117','HLA-A*01:118','HLA-A*01:119','HLA-A*01:12','HLA-A*01:120','HLA-A*01:121','HLA-A*01:122','HLA-A*01:124','HLA-A*01:125','HLA-A*01:126','HLA-A*01:127','HLA-A*01:128','HLA-A*01:129','HLA-A*01:13','HLA-A*01:130','HLA-A*01:131','HLA-A*01:132','HLA-A*01:133','HLA-A*01:134','HLA-A*01:135','HLA-A*01:136','HLA-A*01:137','HLA-A*01:138','HLA-A*01:139','HLA-A*01:14','HLA-A*01:140','HLA-A*01:141','HLA-A*01:142','HLA-A*01:143','HLA-A*01:144','HLA-A*01:145','HLA-A*01:146','HLA-A*01:148','HLA-A*01:149','HLA-A*01:150','HLA-A*01:151','HLA-A*01:152','HLA-A*01:153','HLA-A*01:154','HLA-A*01:155','HLA-A*01:156','HLA-A*01:157','HLA-A*01:158','HLA-A*01:159','HLA-A*01:161','HLA-A*01:163','HLA-A*01:164','HLA-A*01:165','HLA-A*01:166','HLA-A*01:167','HLA-A*01:168','HLA-A*01:169','HLA-A*01:17','HLA-A*01:170','HLA-A*01:171','HLA-A*01:172','HLA-A*01:173','HLA-A*01:174','HLA-A*01:175','HLA-A*01:176','HLA-A*01:177','HLA-A*01:180','HLA-A*01:181','HLA-A*01:182','HLA-A*01:183','HLA-A*01:184','HLA-A*01:185','HLA-A*01:187','HLA-A*01:188','HLA-A*01:189','HLA-A*01:19','HLA-A*01:190','HLA-A*01:191','HLA-A*01:192','HLA-A*01:193','HLA-A*01:194','HLA-A*01:195','HLA-A*01:196','HLA-A*01:197','HLA-A*01:198','HLA-A*01:199','HLA-A*01:20','HLA-A*01:200','HLA-A*01:201','HLA-A*01:202','HLA-A*01:203','HLA-A*01:204','HLA-A*01:205','HLA-A*01:206','HLA-A*01:207','HLA-A*01:209','HLA-A*01:21','HLA-A*01:210','HLA-A*01:211','HLA-A*01:212','HLA-A*01:213','HLA-A*01:214','HLA-A*01:215','HLA-A*01:216','HLA-A*01:217','HLA-A*01:218','HLA-A*01:219','HLA-A*01:220','HLA-A*01:221','HLA-A*01:222','HLA-A*01:223','HLA-A*01:224','HLA-A*01:225','HLA-A*01:226','HLA-A*01:227','HLA-A*01:229','HLA-A*01:23','HLA-A*01:230','HLA-A*01:231','HLA-A*01:232','HLA-A*01:233','HLA-A*01:234','HLA-A*01:235','HLA-A*01:236','HLA-A*01:237','HLA-A*01:238','HLA-A*01:239','HLA-A*01:24','HLA-A*01:241','HLA-A*01:242','HLA-A*01:243','HLA-A*01:244','HLA-A*01:245','HLA-A*01:246','HLA-A*01:249','HLA-A*01:25','HLA-A*01:251','HLA-A*01:252','HLA-A*01:253','HLA-A*01:254','HLA-A*01:255','HLA-A*01:256','HLA-A*01:257','HLA-A*01:259','HLA-A*01:26','HLA-A*01:260','HLA-A*01:261','HLA-A*01:262','HLA-A*01:263','HLA-A*01:264','HLA-A*01:265','HLA-A*01:266','HLA-A*01:267','HLA-A*01:268','HLA-A*01:270','HLA-A*01:271','HLA-A*01:272','HLA-A*01:273','HLA-A*01:274','HLA-A*01:275','HLA-A*01:276','HLA-A*01:277','HLA-A*01:278','HLA-A*01:279','HLA-A*01:28','HLA-A*01:280','HLA-A*01:281','HLA-A*01:282','HLA-A*01:283','HLA-A*01:284','HLA-A*01:286','HLA-A*01:288','HLA-A*01:289','HLA-A*01:29','HLA-A*01:291','HLA-A*01:292','HLA-A*01:294','HLA-A*01:295','HLA-A*01:296','HLA-A*01:297','HLA-A*01:30','HLA-A*01:32','HLA-A*01:33','HLA-A*01:35','HLA-A*01:36','HLA-A*01:37','HLA-A*01:38','HLA-A*01:39','HLA-A*01:40','HLA-A*01:41','HLA-A*01:42','HLA-A*01:43','HLA-A*01:44','HLA-A*01:45','HLA-A*01:46','HLA-A*01:47','HLA-A*01:48','HLA-A*01:49','HLA-A*01:50','HLA-A*01:51','HLA-A*01:54','HLA-A*01:55','HLA-A*01:58','HLA-A*01:59','HLA-A*01:60','HLA-A*01:61','HLA-A*01:62','HLA-A*01:63','HLA-A*01:64','HLA-A*01:65','HLA-A*01:66','HLA-A*01:67','HLA-A*01:68','HLA-A*01:69','HLA-A*01:70','HLA-A*01:71','HLA-A*01:72','HLA-A*01:73','HLA-A*01:74','HLA-A*01:75','HLA-A*01:76','HLA-A*01:77','HLA-A*01:78','HLA-A*01:79','HLA-A*01:80','HLA-A*01:81','HLA-A*01:82','HLA-A*01:83','HLA-A*01:84','HLA-A*01:85','HLA-A*01:86','HLA-A*01:88','HLA-A*01:89','HLA-A*01:90','HLA-A*01:91','HLA-A*01:92','HLA-A*01:93','HLA-A*01:94','HLA-A*01:95','HLA-A*01:96','HLA-A*01:97','HLA-A*01:98','HLA-A*01:99','HLA-A*02:01','HLA-A*02:02','HLA-A*02:03','HLA-A*02:04','HLA-A*02:05','HLA-A*02:06','HLA-A*02:07','HLA-A*02:08','HLA-A*02:09','HLA-A*02:10','HLA-A*02:101','HLA-A*02:102','HLA-A*02:103','HLA-A*02:104','HLA-A*02:105','HLA-A*02:106','HLA-A*02:107','HLA-A*02:108','HLA-A*02:109','HLA-A*02:11','HLA-A*02:110','HLA-A*02:111','HLA-A*02:112','HLA-A*02:114','HLA-A*02:115','HLA-A*02:116','HLA-A*02:117','HLA-A*02:118','HLA-A*02:119','HLA-A*02:12','HLA-A*02:120','HLA-A*02:121','HLA-A*02:122','HLA-A*02:123','HLA-A*02:124','HLA-A*02:126','HLA-A*02:127','HLA-A*02:128','HLA-A*02:129','HLA-A*02:13','HLA-A*02:130','HLA-A*02:131','HLA-A*02:132','HLA-A*02:133','HLA-A*02:134','HLA-A*02:135','HLA-A*02:136','HLA-A*02:137','HLA-A*02:138','HLA-A*02:139','HLA-A*02:14','HLA-A*02:140','HLA-A*02:141','HLA-A*02:142','HLA-A*02:143','HLA-A*02:144','HLA-A*02:145','HLA-A*02:146','HLA-A*02:147','HLA-A*02:148','HLA-A*02:149','HLA-A*02:150','HLA-A*02:151','HLA-A*02:152','HLA-A*02:153','HLA-A*02:154','HLA-A*02:155','HLA-A*02:156','HLA-A*02:157','HLA-A*02:158','HLA-A*02:159','HLA-A*02:16','HLA-A*02:160','HLA-A*02:161','HLA-A*02:162','HLA-A*02:163','HLA-A*02:164','HLA-A*02:165','HLA-A*02:166','HLA-A*02:167','HLA-A*02:168','HLA-A*02:169','HLA-A*02:17','HLA-A*02:170','HLA-A*02:171','HLA-A*02:172','HLA-A*02:173','HLA-A*02:174','HLA-A*02:175','HLA-A*02:176','HLA-A*02:177','HLA-A*02:178','HLA-A*02:179','HLA-A*02:18','HLA-A*02:180','HLA-A*02:181','HLA-A*02:182','HLA-A*02:183','HLA-A*02:184','HLA-A*02:185','HLA-A*02:186','HLA-A*02:187','HLA-A*02:188','HLA-A*02:189','HLA-A*02:19','HLA-A*02:190','HLA-A*02:191','HLA-A*02:192','HLA-A*02:193','HLA-A*02:194','HLA-A*02:195','HLA-A*02:196','HLA-A*02:197','HLA-A*02:198','HLA-A*02:199','HLA-A*02:20','HLA-A*02:200','HLA-A*02:201','HLA-A*02:202','HLA-A*02:203','HLA-A*02:204','HLA-A*02:205','HLA-A*02:206','HLA-A*02:207','HLA-A*02:208','HLA-A*02:209','HLA-A*02:21','HLA-A*02:210','HLA-A*02:211','HLA-A*02:212','HLA-A*02:213','HLA-A*02:214','HLA-A*02:215','HLA-A*02:216','HLA-A*02:217','HLA-A*02:218','HLA-A*02:219','HLA-A*02:22','HLA-A*02:220','HLA-A*02:221','HLA-A*02:224','HLA-A*02:228','HLA-A*02:229','HLA-A*02:230','HLA-A*02:231','HLA-A*02:232','HLA-A*02:233','HLA-A*02:234','HLA-A*02:235','HLA-A*02:236','HLA-A*02:237','HLA-A*02:238','HLA-A*02:239','HLA-A*02:24','HLA-A*02:240','HLA-A*02:241','HLA-A*02:242','HLA-A*02:243','HLA-A*02:244','HLA-A*02:245','HLA-A*02:246','HLA-A*02:247','HLA-A*02:248','HLA-A*02:249','HLA-A*02:25','HLA-A*02:251','HLA-A*02:252','HLA-A*02:253','HLA-A*02:254','HLA-A*02:255','HLA-A*02:256','HLA-A*02:257','HLA-A*02:258','HLA-A*02:259','HLA-A*02:26','HLA-A*02:260','HLA-A*02:261','HLA-A*02:262','HLA-A*02:263','HLA-A*02:264','HLA-A*02:265','HLA-A*02:266','HLA-A*02:267','HLA-A*02:268','HLA-A*02:269','HLA-A*02:27','HLA-A*02:270','HLA-A*02:271','HLA-A*02:272','HLA-A*02:273','HLA-A*02:274','HLA-A*02:275','HLA-A*02:276','HLA-A*02:277','HLA-A*02:278','HLA-A*02:279','HLA-A*02:28','HLA-A*02:280','HLA-A*02:281','HLA-A*02:282','HLA-A*02:283','HLA-A*02:285','HLA-A*02:286','HLA-A*02:287','HLA-A*02:288','HLA-A*02:289','HLA-A*02:29','HLA-A*02:290','HLA-A*02:291','HLA-A*02:292','HLA-A*02:294','HLA-A*02:295','HLA-A*02:296','HLA-A*02:297','HLA-A*02:298','HLA-A*02:299','HLA-A*02:30','HLA-A*02:300','HLA-A*02:302','HLA-A*02:303','HLA-A*02:304','HLA-A*02:306','HLA-A*02:307','HLA-A*02:308','HLA-A*02:309','HLA-A*02:31','HLA-A*02:310','HLA-A*02:311','HLA-A*02:312','HLA-A*02:313','HLA-A*02:315','HLA-A*02:316','HLA-A*02:317','HLA-A*02:318','HLA-A*02:319','HLA-A*02:320','HLA-A*02:322','HLA-A*02:323','HLA-A*02:324','HLA-A*02:325','HLA-A*02:326','HLA-A*02:327','HLA-A*02:328','HLA-A*02:329','HLA-A*02:33','HLA-A*02:330','HLA-A*02:331','HLA-A*02:332','HLA-A*02:333','HLA-A*02:334','HLA-A*02:335','HLA-A*02:336','HLA-A*02:337','HLA-A*02:338','HLA-A*02:339','HLA-A*02:34','HLA-A*02:340','HLA-A*02:341','HLA-A*02:342','HLA-A*02:343','HLA-A*02:344','HLA-A*02:345','HLA-A*02:346','HLA-A*02:347','HLA-A*02:348','HLA-A*02:349','HLA-A*02:35','HLA-A*02:351','HLA-A*02:352','HLA-A*02:353','HLA-A*02:354','HLA-A*02:355','HLA-A*02:357','HLA-A*02:358','HLA-A*02:359','HLA-A*02:36','HLA-A*02:360','HLA-A*02:361','HLA-A*02:362','HLA-A*02:363','HLA-A*02:364','HLA-A*02:365','HLA-A*02:367','HLA-A*02:368','HLA-A*02:369','HLA-A*02:37','HLA-A*02:370','HLA-A*02:371','HLA-A*02:372','HLA-A*02:374','HLA-A*02:375','HLA-A*02:376','HLA-A*02:377','HLA-A*02:378','HLA-A*02:379','HLA-A*02:38','HLA-A*02:380','HLA-A*02:381','HLA-A*02:382','HLA-A*02:383','HLA-A*02:384','HLA-A*02:385','HLA-A*02:386','HLA-A*02:387','HLA-A*02:388','HLA-A*02:389','HLA-A*02:39','HLA-A*02:390','HLA-A*02:391','HLA-A*02:392','HLA-A*02:393','HLA-A*02:394','HLA-A*02:396','HLA-A*02:397','HLA-A*02:398','HLA-A*02:399','HLA-A*02:40','HLA-A*02:400','HLA-A*02:401','HLA-A*02:402','HLA-A*02:403','HLA-A*02:404','HLA-A*02:405','HLA-A*02:406','HLA-A*02:407','HLA-A*02:408','HLA-A*02:409','HLA-A*02:41','HLA-A*02:410','HLA-A*02:411','HLA-A*02:412','HLA-A*02:413','HLA-A*02:414','HLA-A*02:415','HLA-A*02:416','HLA-A*02:417','HLA-A*02:418','HLA-A*02:419','HLA-A*02:42','HLA-A*02:420','HLA-A*02:421','HLA-A*02:422','HLA-A*02:423','HLA-A*02:424','HLA-A*02:425','HLA-A*02:426','HLA-A*02:427','HLA-A*02:428','HLA-A*02:429','HLA-A*02:430','HLA-A*02:431','HLA-A*02:432','HLA-A*02:433','HLA-A*02:434','HLA-A*02:435','HLA-A*02:436','HLA-A*02:437','HLA-A*02:438','HLA-A*02:44','HLA-A*02:441','HLA-A*02:442','HLA-A*02:443','HLA-A*02:444','HLA-A*02:445','HLA-A*02:446','HLA-A*02:447','HLA-A*02:448','HLA-A*02:449','HLA-A*02:45','HLA-A*02:450','HLA-A*02:451','HLA-A*02:452','HLA-A*02:453','HLA-A*02:454','HLA-A*02:455','HLA-A*02:456','HLA-A*02:457','HLA-A*02:458','HLA-A*02:459','HLA-A*02:46','HLA-A*02:460','HLA-A*02:461','HLA-A*02:462','HLA-A*02:463','HLA-A*02:464','HLA-A*02:465','HLA-A*02:466','HLA-A*02:467','HLA-A*02:469','HLA-A*02:47','HLA-A*02:470','HLA-A*02:471','HLA-A*02:472','HLA-A*02:473','HLA-A*02:474','HLA-A*02:475','HLA-A*02:477','HLA-A*02:478','HLA-A*02:479','HLA-A*02:48','HLA-A*02:480','HLA-A*02:481','HLA-A*02:482','HLA-A*02:483','HLA-A*02:484','HLA-A*02:485','HLA-A*02:486','HLA-A*02:487','HLA-A*02:488','HLA-A*02:489','HLA-A*02:49','HLA-A*02:491','HLA-A*02:492','HLA-A*02:493','HLA-A*02:494','HLA-A*02:495','HLA-A*02:496','HLA-A*02:497','HLA-A*02:498','HLA-A*02:499','HLA-A*02:50','HLA-A*02:502','HLA-A*02:503','HLA-A*02:504','HLA-A*02:505','HLA-A*02:507','HLA-A*02:508','HLA-A*02:509','HLA-A*02:51','HLA-A*02:510','HLA-A*02:511','HLA-A*02:512','HLA-A*02:513','HLA-A*02:515','HLA-A*02:517','HLA-A*02:518','HLA-A*02:519','HLA-A*02:52','HLA-A*02:520','HLA-A*02:521','HLA-A*02:522','HLA-A*02:523','HLA-A*02:524','HLA-A*02:526','HLA-A*02:527','HLA-A*02:528','HLA-A*02:529','HLA-A*02:530','HLA-A*02:531','HLA-A*02:532','HLA-A*02:533','HLA-A*02:534','HLA-A*02:535','HLA-A*02:536','HLA-A*02:537','HLA-A*02:538','HLA-A*02:539','HLA-A*02:54','HLA-A*02:541','HLA-A*02:542','HLA-A*02:543','HLA-A*02:544','HLA-A*02:545','HLA-A*02:546','HLA-A*02:547','HLA-A*02:548','HLA-A*02:549','HLA-A*02:55','HLA-A*02:550','HLA-A*02:551','HLA-A*02:552','HLA-A*02:553','HLA-A*02:554','HLA-A*02:555','HLA-A*02:556','HLA-A*02:557','HLA-A*02:558','HLA-A*02:559','HLA-A*02:56','HLA-A*02:560','HLA-A*02:561','HLA-A*02:562','HLA-A*02:563','HLA-A*02:564','HLA-A*02:565','HLA-A*02:566','HLA-A*02:567','HLA-A*02:568','HLA-A*02:569','HLA-A*02:57','HLA-A*02:570','HLA-A*02:571','HLA-A*02:572','HLA-A*02:573','HLA-A*02:574','HLA-A*02:575','HLA-A*02:576','HLA-A*02:577','HLA-A*02:578','HLA-A*02:579','HLA-A*02:58','HLA-A*02:580','HLA-A*02:581','HLA-A*02:582','HLA-A*02:583','HLA-A*02:584','HLA-A*02:585','HLA-A*02:586','HLA-A*02:587','HLA-A*02:588','HLA-A*02:589','HLA-A*02:59','HLA-A*02:590','HLA-A*02:591','HLA-A*02:592','HLA-A*02:593','HLA-A*02:594','HLA-A*02:595','HLA-A*02:596','HLA-A*02:597','HLA-A*02:598','HLA-A*02:599','HLA-A*02:60','HLA-A*02:600','HLA-A*02:601','HLA-A*02:602','HLA-A*02:603','HLA-A*02:604','HLA-A*02:606','HLA-A*02:607','HLA-A*02:609','HLA-A*02:61','HLA-A*02:610','HLA-A*02:611','HLA-A*02:612','HLA-A*02:613','HLA-A*02:614','HLA-A*02:615','HLA-A*02:616','HLA-A*02:617','HLA-A*02:619','HLA-A*02:62','HLA-A*02:620','HLA-A*02:621','HLA-A*02:623','HLA-A*02:624','HLA-A*02:625','HLA-A*02:626','HLA-A*02:627','HLA-A*02:628','HLA-A*02:629','HLA-A*02:63','HLA-A*02:630','HLA-A*02:631','HLA-A*02:632','HLA-A*02:633','HLA-A*02:634','HLA-A*02:635','HLA-A*02:636','HLA-A*02:637','HLA-A*02:638','HLA-A*02:639','HLA-A*02:64','HLA-A*02:640','HLA-A*02:641','HLA-A*02:642','HLA-A*02:644','HLA-A*02:645','HLA-A*02:646','HLA-A*02:647','HLA-A*02:648','HLA-A*02:649','HLA-A*02:65','HLA-A*02:650','HLA-A*02:651','HLA-A*02:652','HLA-A*02:653','HLA-A*02:654','HLA-A*02:655','HLA-A*02:656','HLA-A*02:657','HLA-A*02:658','HLA-A*02:659','HLA-A*02:66','HLA-A*02:660','HLA-A*02:661','HLA-A*02:662','HLA-A*02:663','HLA-A*02:664','HLA-A*02:665','HLA-A*02:666','HLA-A*02:667','HLA-A*02:668','HLA-A*02:669','HLA-A*02:67','HLA-A*02:670','HLA-A*02:671','HLA-A*02:673','HLA-A*02:674','HLA-A*02:676','HLA-A*02:677','HLA-A*02:678','HLA-A*02:679','HLA-A*02:68','HLA-A*02:680','HLA-A*02:681','HLA-A*02:682','HLA-A*02:683','HLA-A*02:684','HLA-A*02:685','HLA-A*02:686','HLA-A*02:687','HLA-A*02:688','HLA-A*02:689','HLA-A*02:69','HLA-A*02:690','HLA-A*02:692','HLA-A*02:693','HLA-A*02:694','HLA-A*02:695','HLA-A*02:697','HLA-A*02:698','HLA-A*02:699','HLA-A*02:70','HLA-A*02:700','HLA-A*02:701','HLA-A*02:702','HLA-A*02:703','HLA-A*02:704','HLA-A*02:705','HLA-A*02:706','HLA-A*02:707','HLA-A*02:708','HLA-A*02:709','HLA-A*02:71','HLA-A*02:711','HLA-A*02:712','HLA-A*02:713','HLA-A*02:714','HLA-A*02:716','HLA-A*02:717','HLA-A*02:718','HLA-A*02:719','HLA-A*02:72','HLA-A*02:720','HLA-A*02:721','HLA-A*02:722','HLA-A*02:723','HLA-A*02:724','HLA-A*02:725','HLA-A*02:726','HLA-A*02:727','HLA-A*02:728','HLA-A*02:729','HLA-A*02:73','HLA-A*02:730','HLA-A*02:731','HLA-A*02:732','HLA-A*02:733','HLA-A*02:734','HLA-A*02:735','HLA-A*02:736','HLA-A*02:737','HLA-A*02:738','HLA-A*02:739','HLA-A*02:74','HLA-A*02:740','HLA-A*02:741','HLA-A*02:742','HLA-A*02:743','HLA-A*02:744','HLA-A*02:745','HLA-A*02:746','HLA-A*02:747','HLA-A*02:749','HLA-A*02:75','HLA-A*02:750','HLA-A*02:751','HLA-A*02:752','HLA-A*02:753','HLA-A*02:754','HLA-A*02:755','HLA-A*02:756','HLA-A*02:757','HLA-A*02:758','HLA-A*02:759','HLA-A*02:76','HLA-A*02:761','HLA-A*02:762','HLA-A*02:763','HLA-A*02:764','HLA-A*02:765','HLA-A*02:766','HLA-A*02:767','HLA-A*02:768','HLA-A*02:769','HLA-A*02:77','HLA-A*02:770','HLA-A*02:771','HLA-A*02:772','HLA-A*02:774','HLA-A*02:776','HLA-A*02:777','HLA-A*02:778','HLA-A*02:779','HLA-A*02:78','HLA-A*02:780','HLA-A*02:781','HLA-A*02:782','HLA-A*02:783','HLA-A*02:784','HLA-A*02:785','HLA-A*02:786','HLA-A*02:787','HLA-A*02:79','HLA-A*02:790','HLA-A*02:794','HLA-A*02:795','HLA-A*02:798','HLA-A*02:799','HLA-A*02:80','HLA-A*02:800','HLA-A*02:801','HLA-A*02:802','HLA-A*02:804','HLA-A*02:808','HLA-A*02:809','HLA-A*02:81','HLA-A*02:810','HLA-A*02:811','HLA-A*02:812','HLA-A*02:813','HLA-A*02:814','HLA-A*02:815','HLA-A*02:816','HLA-A*02:817','HLA-A*02:818','HLA-A*02:819','HLA-A*02:820','HLA-A*02:821','HLA-A*02:822','HLA-A*02:823','HLA-A*02:824','HLA-A*02:825','HLA-A*02:84','HLA-A*02:85','HLA-A*02:86','HLA-A*02:87','HLA-A*02:89','HLA-A*02:90','HLA-A*02:91','HLA-A*02:92','HLA-A*02:93','HLA-A*02:95','HLA-A*02:96','HLA-A*02:97','HLA-A*02:99','HLA-A*03:01','HLA-A*03:02','HLA-A*03:04','HLA-A*03:05','HLA-A*03:06','HLA-A*03:07','HLA-A*03:08','HLA-A*03:09','HLA-A*03:10','HLA-A*03:100','HLA-A*03:101','HLA-A*03:102','HLA-A*03:103','HLA-A*03:104','HLA-A*03:105','HLA-A*03:106','HLA-A*03:107','HLA-A*03:108','HLA-A*03:109','HLA-A*03:110','HLA-A*03:111','HLA-A*03:112','HLA-A*03:113','HLA-A*03:114','HLA-A*03:115','HLA-A*03:116','HLA-A*03:117','HLA-A*03:118','HLA-A*03:119','HLA-A*03:12','HLA-A*03:120','HLA-A*03:121','HLA-A*03:122','HLA-A*03:123','HLA-A*03:124','HLA-A*03:125','HLA-A*03:126','HLA-A*03:127','HLA-A*03:128','HLA-A*03:13','HLA-A*03:130','HLA-A*03:131','HLA-A*03:132','HLA-A*03:133','HLA-A*03:134','HLA-A*03:135','HLA-A*03:136','HLA-A*03:137','HLA-A*03:138','HLA-A*03:139','HLA-A*03:14','HLA-A*03:140','HLA-A*03:141','HLA-A*03:142','HLA-A*03:143','HLA-A*03:144','HLA-A*03:145','HLA-A*03:146','HLA-A*03:147','HLA-A*03:148','HLA-A*03:149','HLA-A*03:15','HLA-A*03:150','HLA-A*03:151','HLA-A*03:152','HLA-A*03:153','HLA-A*03:154','HLA-A*03:155','HLA-A*03:156','HLA-A*03:157','HLA-A*03:158','HLA-A*03:159','HLA-A*03:16','HLA-A*03:160','HLA-A*03:163','HLA-A*03:164','HLA-A*03:165','HLA-A*03:166','HLA-A*03:167','HLA-A*03:169','HLA-A*03:17','HLA-A*03:170','HLA-A*03:171','HLA-A*03:172','HLA-A*03:173','HLA-A*03:174','HLA-A*03:175','HLA-A*03:176','HLA-A*03:177','HLA-A*03:179','HLA-A*03:18','HLA-A*03:180','HLA-A*03:181','HLA-A*03:182','HLA-A*03:183','HLA-A*03:184','HLA-A*03:185','HLA-A*03:186','HLA-A*03:187','HLA-A*03:188','HLA-A*03:189','HLA-A*03:19','HLA-A*03:190','HLA-A*03:191','HLA-A*03:193','HLA-A*03:195','HLA-A*03:196','HLA-A*03:198','HLA-A*03:199','HLA-A*03:20','HLA-A*03:201','HLA-A*03:202','HLA-A*03:203','HLA-A*03:204','HLA-A*03:205','HLA-A*03:206','HLA-A*03:207','HLA-A*03:208','HLA-A*03:209','HLA-A*03:210','HLA-A*03:211','HLA-A*03:212','HLA-A*03:213','HLA-A*03:214','HLA-A*03:215','HLA-A*03:216','HLA-A*03:217','HLA-A*03:218','HLA-A*03:219','HLA-A*03:22','HLA-A*03:220','HLA-A*03:221','HLA-A*03:222','HLA-A*03:223','HLA-A*03:224','HLA-A*03:225','HLA-A*03:226','HLA-A*03:227','HLA-A*03:228','HLA-A*03:229','HLA-A*03:23','HLA-A*03:230','HLA-A*03:231','HLA-A*03:232','HLA-A*03:233','HLA-A*03:235','HLA-A*03:236','HLA-A*03:237','HLA-A*03:238','HLA-A*03:239','HLA-A*03:24','HLA-A*03:240','HLA-A*03:241','HLA-A*03:242','HLA-A*03:243','HLA-A*03:244','HLA-A*03:245','HLA-A*03:246','HLA-A*03:247','HLA-A*03:248','HLA-A*03:249','HLA-A*03:25','HLA-A*03:250','HLA-A*03:251','HLA-A*03:252','HLA-A*03:253','HLA-A*03:254','HLA-A*03:255','HLA-A*03:256','HLA-A*03:257','HLA-A*03:258','HLA-A*03:259','HLA-A*03:26','HLA-A*03:260','HLA-A*03:261','HLA-A*03:263','HLA-A*03:264','HLA-A*03:265','HLA-A*03:267','HLA-A*03:268','HLA-A*03:27','HLA-A*03:270','HLA-A*03:271','HLA-A*03:272','HLA-A*03:273','HLA-A*03:274','HLA-A*03:276','HLA-A*03:277','HLA-A*03:278','HLA-A*03:28','HLA-A*03:280','HLA-A*03:281','HLA-A*03:282','HLA-A*03:285','HLA-A*03:287','HLA-A*03:288','HLA-A*03:289','HLA-A*03:29','HLA-A*03:290','HLA-A*03:291','HLA-A*03:292','HLA-A*03:293','HLA-A*03:294','HLA-A*03:295','HLA-A*03:296','HLA-A*03:298','HLA-A*03:299','HLA-A*03:30','HLA-A*03:300','HLA-A*03:301','HLA-A*03:302','HLA-A*03:303','HLA-A*03:304','HLA-A*03:305','HLA-A*03:306','HLA-A*03:307','HLA-A*03:308','HLA-A*03:309','HLA-A*03:31','HLA-A*03:310','HLA-A*03:311','HLA-A*03:312','HLA-A*03:313','HLA-A*03:314','HLA-A*03:315','HLA-A*03:316','HLA-A*03:317','HLA-A*03:318','HLA-A*03:319','HLA-A*03:32','HLA-A*03:320','HLA-A*03:321','HLA-A*03:322','HLA-A*03:324','HLA-A*03:325','HLA-A*03:326','HLA-A*03:327','HLA-A*03:328','HLA-A*03:33','HLA-A*03:331','HLA-A*03:332','HLA-A*03:333','HLA-A*03:34','HLA-A*03:35','HLA-A*03:37','HLA-A*03:38','HLA-A*03:39','HLA-A*03:40','HLA-A*03:41','HLA-A*03:42','HLA-A*03:43','HLA-A*03:44','HLA-A*03:45','HLA-A*03:46','HLA-A*03:47','HLA-A*03:48','HLA-A*03:49','HLA-A*03:50','HLA-A*03:51','HLA-A*03:52','HLA-A*03:53','HLA-A*03:54','HLA-A*03:55','HLA-A*03:56','HLA-A*03:57','HLA-A*03:58','HLA-A*03:59','HLA-A*03:60','HLA-A*03:61','HLA-A*03:62','HLA-A*03:63','HLA-A*03:64','HLA-A*03:65','HLA-A*03:66','HLA-A*03:67','HLA-A*03:70','HLA-A*03:71','HLA-A*03:72','HLA-A*03:73','HLA-A*03:74','HLA-A*03:75','HLA-A*03:76','HLA-A*03:77','HLA-A*03:78','HLA-A*03:79','HLA-A*03:80','HLA-A*03:81','HLA-A*03:82','HLA-A*03:83','HLA-A*03:84','HLA-A*03:85','HLA-A*03:86','HLA-A*03:87','HLA-A*03:88','HLA-A*03:89','HLA-A*03:90','HLA-A*03:92','HLA-A*03:93','HLA-A*03:94','HLA-A*03:95','HLA-A*03:96','HLA-A*03:97','HLA-A*03:98','HLA-A*03:99','HLA-A*11:01','HLA-A*11:02','HLA-A*11:03','HLA-A*11:04','HLA-A*11:05','HLA-A*11:06','HLA-A*11:07','HLA-A*11:08','HLA-A*11:09','HLA-A*11:10','HLA-A*11:100','HLA-A*11:101','HLA-A*11:102','HLA-A*11:103','HLA-A*11:104','HLA-A*11:105','HLA-A*11:106','HLA-A*11:107','HLA-A*11:108','HLA-A*11:11','HLA-A*11:110','HLA-A*11:111','HLA-A*11:112','HLA-A*11:113','HLA-A*11:114','HLA-A*11:116','HLA-A*11:117','HLA-A*11:118','HLA-A*11:119','HLA-A*11:12','HLA-A*11:120','HLA-A*11:121','HLA-A*11:122','HLA-A*11:123','HLA-A*11:124','HLA-A*11:125','HLA-A*11:126','HLA-A*11:128','HLA-A*11:129','HLA-A*11:13','HLA-A*11:130','HLA-A*11:131','HLA-A*11:132','HLA-A*11:133','HLA-A*11:134','HLA-A*11:135','HLA-A*11:136','HLA-A*11:138','HLA-A*11:139','HLA-A*11:14','HLA-A*11:140','HLA-A*11:141','HLA-A*11:142','HLA-A*11:143','HLA-A*11:144','HLA-A*11:145','HLA-A*11:146','HLA-A*11:147','HLA-A*11:148','HLA-A*11:149','HLA-A*11:15','HLA-A*11:150','HLA-A*11:151','HLA-A*11:152','HLA-A*11:153','HLA-A*11:154','HLA-A*11:155','HLA-A*11:156','HLA-A*11:157','HLA-A*11:158','HLA-A*11:159','HLA-A*11:16','HLA-A*11:160','HLA-A*11:161','HLA-A*11:162','HLA-A*11:163','HLA-A*11:164','HLA-A*11:165','HLA-A*11:166','HLA-A*11:167','HLA-A*11:168','HLA-A*11:169','HLA-A*11:17','HLA-A*11:171','HLA-A*11:172','HLA-A*11:173','HLA-A*11:174','HLA-A*11:175','HLA-A*11:176','HLA-A*11:177','HLA-A*11:178','HLA-A*11:179','HLA-A*11:18','HLA-A*11:181','HLA-A*11:183','HLA-A*11:184','HLA-A*11:185','HLA-A*11:186','HLA-A*11:187','HLA-A*11:188','HLA-A*11:189','HLA-A*11:19','HLA-A*11:190','HLA-A*11:191','HLA-A*11:192','HLA-A*11:193','HLA-A*11:194','HLA-A*11:195','HLA-A*11:196','HLA-A*11:197','HLA-A*11:198','HLA-A*11:199','HLA-A*11:20','HLA-A*11:200','HLA-A*11:201','HLA-A*11:202','HLA-A*11:203','HLA-A*11:204','HLA-A*11:205','HLA-A*11:206','HLA-A*11:207','HLA-A*11:209','HLA-A*11:211','HLA-A*11:212','HLA-A*11:213','HLA-A*11:214','HLA-A*11:216','HLA-A*11:217','HLA-A*11:218','HLA-A*11:219','HLA-A*11:22','HLA-A*11:220','HLA-A*11:221','HLA-A*11:222','HLA-A*11:223','HLA-A*11:224','HLA-A*11:225','HLA-A*11:226','HLA-A*11:227','HLA-A*11:228','HLA-A*11:229','HLA-A*11:23','HLA-A*11:230','HLA-A*11:231','HLA-A*11:232','HLA-A*11:233','HLA-A*11:234','HLA-A*11:236','HLA-A*11:237','HLA-A*11:239','HLA-A*11:24','HLA-A*11:240','HLA-A*11:241','HLA-A*11:242','HLA-A*11:243','HLA-A*11:244','HLA-A*11:245','HLA-A*11:246','HLA-A*11:247','HLA-A*11:248','HLA-A*11:249','HLA-A*11:25','HLA-A*11:250','HLA-A*11:252','HLA-A*11:253','HLA-A*11:254','HLA-A*11:255','HLA-A*11:257','HLA-A*11:258','HLA-A*11:259','HLA-A*11:26','HLA-A*11:260','HLA-A*11:261','HLA-A*11:262','HLA-A*11:263','HLA-A*11:264','HLA-A*11:265','HLA-A*11:266','HLA-A*11:267','HLA-A*11:268','HLA-A*11:269','HLA-A*11:27','HLA-A*11:270','HLA-A*11:271','HLA-A*11:273','HLA-A*11:274','HLA-A*11:275','HLA-A*11:276','HLA-A*11:277','HLA-A*11:278','HLA-A*11:279','HLA-A*11:280','HLA-A*11:281','HLA-A*11:282','HLA-A*11:283','HLA-A*11:284','HLA-A*11:285','HLA-A*11:286','HLA-A*11:288','HLA-A*11:289','HLA-A*11:29','HLA-A*11:290','HLA-A*11:291','HLA-A*11:292','HLA-A*11:293','HLA-A*11:294','HLA-A*11:295','HLA-A*11:296','HLA-A*11:297','HLA-A*11:298','HLA-A*11:299','HLA-A*11:30','HLA-A*11:300','HLA-A*11:301','HLA-A*11:302','HLA-A*11:303','HLA-A*11:304','HLA-A*11:305','HLA-A*11:306','HLA-A*11:307','HLA-A*11:308','HLA-A*11:309','HLA-A*11:31','HLA-A*11:311','HLA-A*11:312','HLA-A*11:32','HLA-A*11:33','HLA-A*11:34','HLA-A*11:35','HLA-A*11:36','HLA-A*11:37','HLA-A*11:38','HLA-A*11:39','HLA-A*11:40','HLA-A*11:41','HLA-A*11:42','HLA-A*11:43','HLA-A*11:44','HLA-A*11:45','HLA-A*11:46','HLA-A*11:47','HLA-A*11:48','HLA-A*11:49','HLA-A*11:51','HLA-A*11:53','HLA-A*11:54','HLA-A*11:55','HLA-A*11:56','HLA-A*11:57','HLA-A*11:58','HLA-A*11:59','HLA-A*11:60','HLA-A*11:61','HLA-A*11:62','HLA-A*11:63','HLA-A*11:64','HLA-A*11:65','HLA-A*11:66','HLA-A*11:67','HLA-A*11:68','HLA-A*11:70','HLA-A*11:71','HLA-A*11:72','HLA-A*11:73','HLA-A*11:74','HLA-A*11:75','HLA-A*11:76','HLA-A*11:77','HLA-A*11:79','HLA-A*11:80','HLA-A*11:81','HLA-A*11:82','HLA-A*11:83','HLA-A*11:84','HLA-A*11:85','HLA-A*11:86','HLA-A*11:87','HLA-A*11:88','HLA-A*11:89','HLA-A*11:90','HLA-A*11:91','HLA-A*11:92','HLA-A*11:93','HLA-A*11:94','HLA-A*11:95','HLA-A*11:96','HLA-A*11:97','HLA-A*11:98','HLA-A*23:01','HLA-A*23:02','HLA-A*23:03','HLA-A*23:04','HLA-A*23:05','HLA-A*23:06','HLA-A*23:09','HLA-A*23:10','HLA-A*23:12','HLA-A*23:13','HLA-A*23:14','HLA-A*23:15','HLA-A*23:16','HLA-A*23:17','HLA-A*23:18','HLA-A*23:20','HLA-A*23:21','HLA-A*23:22','HLA-A*23:23','HLA-A*23:24','HLA-A*23:25','HLA-A*23:26','HLA-A*23:27','HLA-A*23:28','HLA-A*23:29','HLA-A*23:30','HLA-A*23:31','HLA-A*23:32','HLA-A*23:33','HLA-A*23:34','HLA-A*23:35','HLA-A*23:36','HLA-A*23:37','HLA-A*23:39','HLA-A*23:40','HLA-A*23:41','HLA-A*23:42','HLA-A*23:43','HLA-A*23:44','HLA-A*23:45','HLA-A*23:46','HLA-A*23:47','HLA-A*23:48','HLA-A*23:49','HLA-A*23:50','HLA-A*23:51','HLA-A*23:52','HLA-A*23:53','HLA-A*23:54','HLA-A*23:55','HLA-A*23:56','HLA-A*23:57','HLA-A*23:58','HLA-A*23:59','HLA-A*23:60','HLA-A*23:61','HLA-A*23:62','HLA-A*23:63','HLA-A*23:64','HLA-A*23:65','HLA-A*23:66','HLA-A*23:67','HLA-A*23:68','HLA-A*23:70','HLA-A*23:71','HLA-A*23:72','HLA-A*23:73','HLA-A*23:74','HLA-A*23:75','HLA-A*23:76','HLA-A*23:77','HLA-A*23:78','HLA-A*23:79','HLA-A*23:80','HLA-A*23:81','HLA-A*23:82','HLA-A*23:83','HLA-A*23:85','HLA-A*23:86','HLA-A*23:87','HLA-A*23:88','HLA-A*23:89','HLA-A*23:90','HLA-A*23:92','HLA-A*24:02','HLA-A*24:03','HLA-A*24:04','HLA-A*24:05','HLA-A*24:06','HLA-A*24:07','HLA-A*24:08','HLA-A*24:10','HLA-A*24:100','HLA-A*24:101','HLA-A*24:102','HLA-A*24:103','HLA-A*24:104','HLA-A*24:105','HLA-A*24:106','HLA-A*24:107','HLA-A*24:108','HLA-A*24:109','HLA-A*24:110','HLA-A*24:111','HLA-A*24:112','HLA-A*24:113','HLA-A*24:114','HLA-A*24:115','HLA-A*24:116','HLA-A*24:117','HLA-A*24:118','HLA-A*24:119','HLA-A*24:120','HLA-A*24:121','HLA-A*24:122','HLA-A*24:123','HLA-A*24:124','HLA-A*24:125','HLA-A*24:126','HLA-A*24:127','HLA-A*24:128','HLA-A*24:129','HLA-A*24:13','HLA-A*24:130','HLA-A*24:131','HLA-A*24:133','HLA-A*24:134','HLA-A*24:135','HLA-A*24:136','HLA-A*24:137','HLA-A*24:138','HLA-A*24:139','HLA-A*24:14','HLA-A*24:140','HLA-A*24:141','HLA-A*24:142','HLA-A*24:143','HLA-A*24:144','HLA-A*24:145','HLA-A*24:146','HLA-A*24:147','HLA-A*24:148','HLA-A*24:149','HLA-A*24:15','HLA-A*24:150','HLA-A*24:151','HLA-A*24:152','HLA-A*24:153','HLA-A*24:154','HLA-A*24:156','HLA-A*24:157','HLA-A*24:159','HLA-A*24:160','HLA-A*24:161','HLA-A*24:162','HLA-A*24:164','HLA-A*24:165','HLA-A*24:166','HLA-A*24:167','HLA-A*24:168','HLA-A*24:169','HLA-A*24:17','HLA-A*24:170','HLA-A*24:171','HLA-A*24:172','HLA-A*24:173','HLA-A*24:174','HLA-A*24:175','HLA-A*24:176','HLA-A*24:177','HLA-A*24:178','HLA-A*24:179','HLA-A*24:18','HLA-A*24:180','HLA-A*24:181','HLA-A*24:182','HLA-A*24:184','HLA-A*24:186','HLA-A*24:187','HLA-A*24:188','HLA-A*24:189','HLA-A*24:19','HLA-A*24:190','HLA-A*24:191','HLA-A*24:192','HLA-A*24:193','HLA-A*24:194','HLA-A*24:195','HLA-A*24:196','HLA-A*24:197','HLA-A*24:198','HLA-A*24:199','HLA-A*24:20','HLA-A*24:200','HLA-A*24:201','HLA-A*24:202','HLA-A*24:203','HLA-A*24:204','HLA-A*24:205','HLA-A*24:206','HLA-A*24:207','HLA-A*24:208','HLA-A*24:209','HLA-A*24:21','HLA-A*24:210','HLA-A*24:212','HLA-A*24:213','HLA-A*24:214','HLA-A*24:215','HLA-A*24:216','HLA-A*24:217','HLA-A*24:218','HLA-A*24:219','HLA-A*24:22','HLA-A*24:220','HLA-A*24:221','HLA-A*24:223','HLA-A*24:224','HLA-A*24:225','HLA-A*24:226','HLA-A*24:227','HLA-A*24:228','HLA-A*24:229','HLA-A*24:23','HLA-A*24:230','HLA-A*24:231','HLA-A*24:233','HLA-A*24:234','HLA-A*24:235','HLA-A*24:236','HLA-A*24:237','HLA-A*24:238','HLA-A*24:239','HLA-A*24:24','HLA-A*24:241','HLA-A*24:242','HLA-A*24:243','HLA-A*24:244','HLA-A*24:245','HLA-A*24:246','HLA-A*24:247','HLA-A*24:248','HLA-A*24:249','HLA-A*24:25','HLA-A*24:250','HLA-A*24:251','HLA-A*24:253','HLA-A*24:254','HLA-A*24:255','HLA-A*24:256','HLA-A*24:257','HLA-A*24:258','HLA-A*24:259','HLA-A*24:26','HLA-A*24:260','HLA-A*24:261','HLA-A*24:262','HLA-A*24:263','HLA-A*24:264','HLA-A*24:265','HLA-A*24:266','HLA-A*24:267','HLA-A*24:268','HLA-A*24:269','HLA-A*24:27','HLA-A*24:270','HLA-A*24:271','HLA-A*24:272','HLA-A*24:273','HLA-A*24:274','HLA-A*24:275','HLA-A*24:276','HLA-A*24:277','HLA-A*24:279','HLA-A*24:28','HLA-A*24:280','HLA-A*24:281','HLA-A*24:282','HLA-A*24:283','HLA-A*24:284','HLA-A*24:285','HLA-A*24:286','HLA-A*24:287','HLA-A*24:288','HLA-A*24:289','HLA-A*24:29','HLA-A*24:290','HLA-A*24:291','HLA-A*24:292','HLA-A*24:293','HLA-A*24:295','HLA-A*24:296','HLA-A*24:297','HLA-A*24:298','HLA-A*24:299','HLA-A*24:30','HLA-A*24:300','HLA-A*24:301','HLA-A*24:302','HLA-A*24:304','HLA-A*24:305','HLA-A*24:306','HLA-A*24:307','HLA-A*24:308','HLA-A*24:309','HLA-A*24:31','HLA-A*24:310','HLA-A*24:311','HLA-A*24:313','HLA-A*24:314','HLA-A*24:315','HLA-A*24:316','HLA-A*24:317','HLA-A*24:318','HLA-A*24:319','HLA-A*24:32','HLA-A*24:320','HLA-A*24:321','HLA-A*24:322','HLA-A*24:324','HLA-A*24:325','HLA-A*24:326','HLA-A*24:327','HLA-A*24:328','HLA-A*24:329','HLA-A*24:33','HLA-A*24:330','HLA-A*24:331','HLA-A*24:332','HLA-A*24:333','HLA-A*24:334','HLA-A*24:335','HLA-A*24:336','HLA-A*24:337','HLA-A*24:338','HLA-A*24:339','HLA-A*24:34','HLA-A*24:340','HLA-A*24:341','HLA-A*24:342','HLA-A*24:343','HLA-A*24:344','HLA-A*24:345','HLA-A*24:346','HLA-A*24:347','HLA-A*24:348','HLA-A*24:349','HLA-A*24:35','HLA-A*24:350','HLA-A*24:351','HLA-A*24:352','HLA-A*24:353','HLA-A*24:354','HLA-A*24:355','HLA-A*24:356','HLA-A*24:358','HLA-A*24:360','HLA-A*24:361','HLA-A*24:362','HLA-A*24:363','HLA-A*24:364','HLA-A*24:365','HLA-A*24:366','HLA-A*24:367','HLA-A*24:368','HLA-A*24:369','HLA-A*24:37','HLA-A*24:371','HLA-A*24:372','HLA-A*24:373','HLA-A*24:374','HLA-A*24:375','HLA-A*24:376','HLA-A*24:377','HLA-A*24:378','HLA-A*24:379','HLA-A*24:38','HLA-A*24:380','HLA-A*24:381','HLA-A*24:382','HLA-A*24:383','HLA-A*24:384','HLA-A*24:385','HLA-A*24:386','HLA-A*24:387','HLA-A*24:39','HLA-A*24:390','HLA-A*24:391','HLA-A*24:392','HLA-A*24:393','HLA-A*24:394','HLA-A*24:395','HLA-A*24:397','HLA-A*24:398','HLA-A*24:399','HLA-A*24:400','HLA-A*24:401','HLA-A*24:402','HLA-A*24:403','HLA-A*24:404','HLA-A*24:405','HLA-A*24:406','HLA-A*24:407','HLA-A*24:409','HLA-A*24:41','HLA-A*24:410','HLA-A*24:411','HLA-A*24:412','HLA-A*24:413','HLA-A*24:414','HLA-A*24:415','HLA-A*24:416','HLA-A*24:417','HLA-A*24:418','HLA-A*24:419','HLA-A*24:42','HLA-A*24:420','HLA-A*24:421','HLA-A*24:422','HLA-A*24:423','HLA-A*24:424','HLA-A*24:43','HLA-A*24:431','HLA-A*24:432','HLA-A*24:44','HLA-A*24:46','HLA-A*24:47','HLA-A*24:49','HLA-A*24:50','HLA-A*24:51','HLA-A*24:52','HLA-A*24:53','HLA-A*24:54','HLA-A*24:55','HLA-A*24:56','HLA-A*24:57','HLA-A*24:58','HLA-A*24:59','HLA-A*24:61','HLA-A*24:62','HLA-A*24:63','HLA-A*24:64','HLA-A*24:66','HLA-A*24:67','HLA-A*24:68','HLA-A*24:69','HLA-A*24:70','HLA-A*24:71','HLA-A*24:72','HLA-A*24:73','HLA-A*24:74','HLA-A*24:75','HLA-A*24:76','HLA-A*24:77','HLA-A*24:78','HLA-A*24:79','HLA-A*24:80','HLA-A*24:81','HLA-A*24:82','HLA-A*24:85','HLA-A*24:87','HLA-A*24:88','HLA-A*24:89','HLA-A*24:91','HLA-A*24:92','HLA-A*24:93','HLA-A*24:94','HLA-A*24:95','HLA-A*24:96','HLA-A*24:97','HLA-A*24:98','HLA-A*24:99','HLA-A*25:01','HLA-A*25:02','HLA-A*25:03','HLA-A*25:04','HLA-A*25:05','HLA-A*25:06','HLA-A*25:07','HLA-A*25:08','HLA-A*25:09','HLA-A*25:10','HLA-A*25:11','HLA-A*25:13','HLA-A*25:14','HLA-A*25:15','HLA-A*25:16','HLA-A*25:17','HLA-A*25:18','HLA-A*25:19','HLA-A*25:20','HLA-A*25:21','HLA-A*25:22','HLA-A*25:23','HLA-A*25:24','HLA-A*25:25','HLA-A*25:26','HLA-A*25:27','HLA-A*25:28','HLA-A*25:29','HLA-A*25:30','HLA-A*25:31','HLA-A*25:32','HLA-A*25:33','HLA-A*25:34','HLA-A*25:35','HLA-A*25:36','HLA-A*25:37','HLA-A*25:38','HLA-A*25:39','HLA-A*25:40','HLA-A*25:41','HLA-A*25:43','HLA-A*25:44','HLA-A*25:45','HLA-A*25:46','HLA-A*25:47','HLA-A*25:48','HLA-A*25:50','HLA-A*25:51','HLA-A*25:52','HLA-A*25:53','HLA-A*25:54','HLA-A*25:55','HLA-A*25:56','HLA-A*25:57','HLA-A*26:01','HLA-A*26:02','HLA-A*26:03','HLA-A*26:04','HLA-A*26:05','HLA-A*26:06','HLA-A*26:07','HLA-A*26:08','HLA-A*26:09','HLA-A*26:10','HLA-A*26:100','HLA-A*26:101','HLA-A*26:102','HLA-A*26:103','HLA-A*26:104','HLA-A*26:105','HLA-A*26:106','HLA-A*26:108','HLA-A*26:109','HLA-A*26:110','HLA-A*26:111','HLA-A*26:112','HLA-A*26:113','HLA-A*26:114','HLA-A*26:115','HLA-A*26:116','HLA-A*26:117','HLA-A*26:118','HLA-A*26:119','HLA-A*26:12','HLA-A*26:120','HLA-A*26:121','HLA-A*26:122','HLA-A*26:123','HLA-A*26:124','HLA-A*26:125','HLA-A*26:126','HLA-A*26:128','HLA-A*26:129','HLA-A*26:13','HLA-A*26:130','HLA-A*26:131','HLA-A*26:132','HLA-A*26:133','HLA-A*26:134','HLA-A*26:135','HLA-A*26:136','HLA-A*26:137','HLA-A*26:138','HLA-A*26:139','HLA-A*26:14','HLA-A*26:140','HLA-A*26:141','HLA-A*26:142','HLA-A*26:143','HLA-A*26:144','HLA-A*26:146','HLA-A*26:147','HLA-A*26:148','HLA-A*26:149','HLA-A*26:15','HLA-A*26:150','HLA-A*26:151','HLA-A*26:152','HLA-A*26:153','HLA-A*26:154','HLA-A*26:155','HLA-A*26:156','HLA-A*26:157','HLA-A*26:158','HLA-A*26:159','HLA-A*26:16','HLA-A*26:160','HLA-A*26:162','HLA-A*26:163','HLA-A*26:164','HLA-A*26:165','HLA-A*26:167','HLA-A*26:168','HLA-A*26:169','HLA-A*26:17','HLA-A*26:170','HLA-A*26:171','HLA-A*26:172','HLA-A*26:173','HLA-A*26:174','HLA-A*26:175','HLA-A*26:176','HLA-A*26:177','HLA-A*26:178','HLA-A*26:18','HLA-A*26:19','HLA-A*26:20','HLA-A*26:21','HLA-A*26:22','HLA-A*26:23','HLA-A*26:24','HLA-A*26:26','HLA-A*26:27','HLA-A*26:28','HLA-A*26:29','HLA-A*26:30','HLA-A*26:31','HLA-A*26:32','HLA-A*26:33','HLA-A*26:34','HLA-A*26:35','HLA-A*26:36','HLA-A*26:37','HLA-A*26:38','HLA-A*26:39','HLA-A*26:40','HLA-A*26:41','HLA-A*26:42','HLA-A*26:43','HLA-A*26:45','HLA-A*26:46','HLA-A*26:47','HLA-A*26:48','HLA-A*26:49','HLA-A*26:50','HLA-A*26:51','HLA-A*26:52','HLA-A*26:53','HLA-A*26:54','HLA-A*26:55','HLA-A*26:56','HLA-A*26:57','HLA-A*26:58','HLA-A*26:59','HLA-A*26:61','HLA-A*26:62','HLA-A*26:63','HLA-A*26:64','HLA-A*26:65','HLA-A*26:66','HLA-A*26:67','HLA-A*26:68','HLA-A*26:69','HLA-A*26:70','HLA-A*26:72','HLA-A*26:73','HLA-A*26:74','HLA-A*26:75','HLA-A*26:76','HLA-A*26:77','HLA-A*26:78','HLA-A*26:79','HLA-A*26:80','HLA-A*26:81','HLA-A*26:82','HLA-A*26:83','HLA-A*26:84','HLA-A*26:85','HLA-A*26:86','HLA-A*26:87','HLA-A*26:88','HLA-A*26:89','HLA-A*26:90','HLA-A*26:91','HLA-A*26:92','HLA-A*26:93','HLA-A*26:94','HLA-A*26:95','HLA-A*26:96','HLA-A*26:97','HLA-A*26:98','HLA-A*26:99','HLA-A*29:01','HLA-A*29:02','HLA-A*29:03','HLA-A*29:04','HLA-A*29:05','HLA-A*29:06','HLA-A*29:07','HLA-A*29:09','HLA-A*29:10','HLA-A*29:100','HLA-A*29:101','HLA-A*29:102','HLA-A*29:103','HLA-A*29:104','HLA-A*29:105','HLA-A*29:106','HLA-A*29:107','HLA-A*29:108','HLA-A*29:109','HLA-A*29:11','HLA-A*29:110','HLA-A*29:111','HLA-A*29:113','HLA-A*29:114','HLA-A*29:115','HLA-A*29:116','HLA-A*29:117','HLA-A*29:118','HLA-A*29:119','HLA-A*29:12','HLA-A*29:120','HLA-A*29:121','HLA-A*29:122','HLA-A*29:123','HLA-A*29:124','HLA-A*29:125','HLA-A*29:127','HLA-A*29:128','HLA-A*29:13','HLA-A*29:14','HLA-A*29:15','HLA-A*29:16','HLA-A*29:17','HLA-A*29:18','HLA-A*29:19','HLA-A*29:20','HLA-A*29:21','HLA-A*29:22','HLA-A*29:23','HLA-A*29:24','HLA-A*29:25','HLA-A*29:26','HLA-A*29:27','HLA-A*29:28','HLA-A*29:29','HLA-A*29:30','HLA-A*29:31','HLA-A*29:32','HLA-A*29:33','HLA-A*29:34','HLA-A*29:35','HLA-A*29:36','HLA-A*29:37','HLA-A*29:38','HLA-A*29:39','HLA-A*29:40','HLA-A*29:41','HLA-A*29:42','HLA-A*29:43','HLA-A*29:44','HLA-A*29:45','HLA-A*29:46','HLA-A*29:47','HLA-A*29:48','HLA-A*29:49','HLA-A*29:50','HLA-A*29:51','HLA-A*29:52','HLA-A*29:53','HLA-A*29:54','HLA-A*29:55','HLA-A*29:56','HLA-A*29:57','HLA-A*29:58','HLA-A*29:59','HLA-A*29:60','HLA-A*29:61','HLA-A*29:62','HLA-A*29:63','HLA-A*29:64','HLA-A*29:65','HLA-A*29:66','HLA-A*29:67','HLA-A*29:68','HLA-A*29:69','HLA-A*29:70','HLA-A*29:71','HLA-A*29:72','HLA-A*29:73','HLA-A*29:74','HLA-A*29:75','HLA-A*29:76','HLA-A*29:77','HLA-A*29:79','HLA-A*29:80','HLA-A*29:81','HLA-A*29:82','HLA-A*29:83','HLA-A*29:84','HLA-A*29:85','HLA-A*29:86','HLA-A*29:87','HLA-A*29:88','HLA-A*29:89','HLA-A*29:90','HLA-A*29:91','HLA-A*29:92','HLA-A*29:93','HLA-A*29:94','HLA-A*29:95','HLA-A*29:96','HLA-A*29:97','HLA-A*29:98','HLA-A*29:99','HLA-A*30:01','HLA-A*30:02','HLA-A*30:03','HLA-A*30:04','HLA-A*30:06','HLA-A*30:07','HLA-A*30:08','HLA-A*30:09','HLA-A*30:10','HLA-A*30:100','HLA-A*30:102','HLA-A*30:103','HLA-A*30:104','HLA-A*30:105','HLA-A*30:106','HLA-A*30:107','HLA-A*30:108','HLA-A*30:109','HLA-A*30:11','HLA-A*30:110','HLA-A*30:111','HLA-A*30:112','HLA-A*30:113','HLA-A*30:114','HLA-A*30:115','HLA-A*30:116','HLA-A*30:117','HLA-A*30:118','HLA-A*30:119','HLA-A*30:12','HLA-A*30:120','HLA-A*30:122','HLA-A*30:124','HLA-A*30:125','HLA-A*30:126','HLA-A*30:127','HLA-A*30:128','HLA-A*30:129','HLA-A*30:13','HLA-A*30:131','HLA-A*30:133','HLA-A*30:134','HLA-A*30:135','HLA-A*30:136','HLA-A*30:137','HLA-A*30:138','HLA-A*30:139','HLA-A*30:140','HLA-A*30:141','HLA-A*30:142','HLA-A*30:143','HLA-A*30:144','HLA-A*30:14L','HLA-A*30:15','HLA-A*30:16','HLA-A*30:17','HLA-A*30:18','HLA-A*30:19','HLA-A*30:20','HLA-A*30:22','HLA-A*30:23','HLA-A*30:24','HLA-A*30:25','HLA-A*30:26','HLA-A*30:28','HLA-A*30:29','HLA-A*30:30','HLA-A*30:31','HLA-A*30:32','HLA-A*30:33','HLA-A*30:34','HLA-A*30:35','HLA-A*30:36','HLA-A*30:37','HLA-A*30:38','HLA-A*30:39','HLA-A*30:40','HLA-A*30:41','HLA-A*30:42','HLA-A*30:43','HLA-A*30:44','HLA-A*30:45','HLA-A*30:46','HLA-A*30:47','HLA-A*30:48','HLA-A*30:49','HLA-A*30:50','HLA-A*30:51','HLA-A*30:52','HLA-A*30:53','HLA-A*30:54','HLA-A*30:55','HLA-A*30:56','HLA-A*30:57','HLA-A*30:58','HLA-A*30:60','HLA-A*30:61','HLA-A*30:62','HLA-A*30:63','HLA-A*30:64','HLA-A*30:65','HLA-A*30:66','HLA-A*30:67','HLA-A*30:68','HLA-A*30:69','HLA-A*30:71','HLA-A*30:72','HLA-A*30:74','HLA-A*30:75','HLA-A*30:77','HLA-A*30:79','HLA-A*30:80','HLA-A*30:81','HLA-A*30:82','HLA-A*30:83','HLA-A*30:84','HLA-A*30:85','HLA-A*30:86','HLA-A*30:87','HLA-A*30:88','HLA-A*30:89','HLA-A*30:90','HLA-A*30:91','HLA-A*30:92','HLA-A*30:93','HLA-A*30:94','HLA-A*30:95','HLA-A*30:96','HLA-A*30:97','HLA-A*30:98','HLA-A*30:99','HLA-A*31:01','HLA-A*31:02','HLA-A*31:03','HLA-A*31:04','HLA-A*31:05','HLA-A*31:06','HLA-A*31:07','HLA-A*31:08','HLA-A*31:09','HLA-A*31:10','HLA-A*31:100','HLA-A*31:101','HLA-A*31:102','HLA-A*31:103','HLA-A*31:104','HLA-A*31:105','HLA-A*31:106','HLA-A*31:107','HLA-A*31:108','HLA-A*31:109','HLA-A*31:11','HLA-A*31:110','HLA-A*31:111','HLA-A*31:112','HLA-A*31:113','HLA-A*31:114','HLA-A*31:115','HLA-A*31:116','HLA-A*31:117','HLA-A*31:118','HLA-A*31:119','HLA-A*31:12','HLA-A*31:120','HLA-A*31:121','HLA-A*31:122','HLA-A*31:123','HLA-A*31:124','HLA-A*31:125','HLA-A*31:127','HLA-A*31:128','HLA-A*31:129','HLA-A*31:13','HLA-A*31:130','HLA-A*31:132','HLA-A*31:133','HLA-A*31:134','HLA-A*31:135','HLA-A*31:136','HLA-A*31:137','HLA-A*31:138','HLA-A*31:139','HLA-A*31:140','HLA-A*31:142','HLA-A*31:143','HLA-A*31:144','HLA-A*31:145','HLA-A*31:146','HLA-A*31:147','HLA-A*31:148','HLA-A*31:15','HLA-A*31:16','HLA-A*31:17','HLA-A*31:18','HLA-A*31:19','HLA-A*31:20','HLA-A*31:21','HLA-A*31:22','HLA-A*31:23','HLA-A*31:24','HLA-A*31:25','HLA-A*31:26','HLA-A*31:27','HLA-A*31:28','HLA-A*31:29','HLA-A*31:30','HLA-A*31:31','HLA-A*31:32','HLA-A*31:33','HLA-A*31:34','HLA-A*31:35','HLA-A*31:36','HLA-A*31:37','HLA-A*31:38','HLA-A*31:39','HLA-A*31:40','HLA-A*31:41','HLA-A*31:42','HLA-A*31:43','HLA-A*31:44','HLA-A*31:45','HLA-A*31:46','HLA-A*31:47','HLA-A*31:48','HLA-A*31:49','HLA-A*31:50','HLA-A*31:51','HLA-A*31:52','HLA-A*31:53','HLA-A*31:54','HLA-A*31:55','HLA-A*31:56','HLA-A*31:57','HLA-A*31:58','HLA-A*31:59','HLA-A*31:61','HLA-A*31:62','HLA-A*31:63','HLA-A*31:64','HLA-A*31:65','HLA-A*31:66','HLA-A*31:67','HLA-A*31:68','HLA-A*31:69','HLA-A*31:70','HLA-A*31:71','HLA-A*31:72','HLA-A*31:73','HLA-A*31:74','HLA-A*31:75','HLA-A*31:76','HLA-A*31:77','HLA-A*31:78','HLA-A*31:79','HLA-A*31:80','HLA-A*31:81','HLA-A*31:82','HLA-A*31:83','HLA-A*31:84','HLA-A*31:85','HLA-A*31:86','HLA-A*31:87','HLA-A*31:88','HLA-A*31:89','HLA-A*31:90','HLA-A*31:91','HLA-A*31:92','HLA-A*31:93','HLA-A*31:94','HLA-A*31:95','HLA-A*31:96','HLA-A*31:97','HLA-A*31:98','HLA-A*31:99','HLA-A*32:01','HLA-A*32:02','HLA-A*32:03','HLA-A*32:04','HLA-A*32:05','HLA-A*32:06','HLA-A*32:07','HLA-A*32:08','HLA-A*32:09','HLA-A*32:10','HLA-A*32:100','HLA-A*32:102','HLA-A*32:103','HLA-A*32:104','HLA-A*32:105','HLA-A*32:106','HLA-A*32:107','HLA-A*32:108','HLA-A*32:109','HLA-A*32:110','HLA-A*32:111','HLA-A*32:113','HLA-A*32:114','HLA-A*32:115','HLA-A*32:116','HLA-A*32:118','HLA-A*32:119','HLA-A*32:12','HLA-A*32:120','HLA-A*32:121','HLA-A*32:13','HLA-A*32:14','HLA-A*32:15','HLA-A*32:16','HLA-A*32:17','HLA-A*32:18','HLA-A*32:20','HLA-A*32:21','HLA-A*32:22','HLA-A*32:23','HLA-A*32:24','HLA-A*32:25','HLA-A*32:26','HLA-A*32:28','HLA-A*32:29','HLA-A*32:30','HLA-A*32:31','HLA-A*32:32','HLA-A*32:33','HLA-A*32:34','HLA-A*32:35','HLA-A*32:36','HLA-A*32:37','HLA-A*32:38','HLA-A*32:39','HLA-A*32:40','HLA-A*32:41','HLA-A*32:42','HLA-A*32:43','HLA-A*32:44','HLA-A*32:46','HLA-A*32:47','HLA-A*32:49','HLA-A*32:50','HLA-A*32:51','HLA-A*32:52','HLA-A*32:53','HLA-A*32:54','HLA-A*32:55','HLA-A*32:57','HLA-A*32:58','HLA-A*32:59','HLA-A*32:60','HLA-A*32:61','HLA-A*32:62','HLA-A*32:63','HLA-A*32:64','HLA-A*32:65','HLA-A*32:66','HLA-A*32:67','HLA-A*32:68','HLA-A*32:69','HLA-A*32:70','HLA-A*32:71','HLA-A*32:72','HLA-A*32:73','HLA-A*32:74','HLA-A*32:75','HLA-A*32:76','HLA-A*32:77','HLA-A*32:78','HLA-A*32:79','HLA-A*32:80','HLA-A*32:81','HLA-A*32:82','HLA-A*32:83','HLA-A*32:84','HLA-A*32:85','HLA-A*32:86','HLA-A*32:87','HLA-A*32:88','HLA-A*32:89','HLA-A*32:90','HLA-A*32:91','HLA-A*32:93','HLA-A*32:94','HLA-A*32:95','HLA-A*32:96','HLA-A*32:97','HLA-A*32:98','HLA-A*32:99','HLA-A*33:01','HLA-A*33:03','HLA-A*33:04','HLA-A*33:05','HLA-A*33:06','HLA-A*33:07','HLA-A*33:08','HLA-A*33:09','HLA-A*33:10','HLA-A*33:100','HLA-A*33:101','HLA-A*33:102','HLA-A*33:103','HLA-A*33:104','HLA-A*33:105','HLA-A*33:106','HLA-A*33:107','HLA-A*33:108','HLA-A*33:109','HLA-A*33:11','HLA-A*33:110','HLA-A*33:111','HLA-A*33:112','HLA-A*33:113','HLA-A*33:114','HLA-A*33:115','HLA-A*33:116','HLA-A*33:117','HLA-A*33:118','HLA-A*33:119','HLA-A*33:12','HLA-A*33:120','HLA-A*33:121','HLA-A*33:122','HLA-A*33:124','HLA-A*33:125','HLA-A*33:126','HLA-A*33:127','HLA-A*33:128','HLA-A*33:13','HLA-A*33:130','HLA-A*33:131','HLA-A*33:132','HLA-A*33:133','HLA-A*33:134','HLA-A*33:135','HLA-A*33:136','HLA-A*33:137','HLA-A*33:138','HLA-A*33:139','HLA-A*33:14','HLA-A*33:141','HLA-A*33:142','HLA-A*33:144','HLA-A*33:145','HLA-A*33:146','HLA-A*33:147','HLA-A*33:148','HLA-A*33:149','HLA-A*33:15','HLA-A*33:150','HLA-A*33:151','HLA-A*33:152','HLA-A*33:153','HLA-A*33:155','HLA-A*33:158','HLA-A*33:159','HLA-A*33:16','HLA-A*33:160','HLA-A*33:161','HLA-A*33:162','HLA-A*33:163','HLA-A*33:164','HLA-A*33:165','HLA-A*33:166','HLA-A*33:167','HLA-A*33:168','HLA-A*33:169','HLA-A*33:17','HLA-A*33:170','HLA-A*33:18','HLA-A*33:19','HLA-A*33:20','HLA-A*33:21','HLA-A*33:22','HLA-A*33:23','HLA-A*33:24','HLA-A*33:25','HLA-A*33:26','HLA-A*33:27','HLA-A*33:28','HLA-A*33:29','HLA-A*33:30','HLA-A*33:31','HLA-A*33:32','HLA-A*33:33','HLA-A*33:34','HLA-A*33:35','HLA-A*33:36','HLA-A*33:37','HLA-A*33:39','HLA-A*33:40','HLA-A*33:41','HLA-A*33:42','HLA-A*33:43','HLA-A*33:44','HLA-A*33:45','HLA-A*33:46','HLA-A*33:47','HLA-A*33:48','HLA-A*33:49','HLA-A*33:50','HLA-A*33:51','HLA-A*33:52','HLA-A*33:53','HLA-A*33:54','HLA-A*33:55','HLA-A*33:56','HLA-A*33:57','HLA-A*33:58','HLA-A*33:59','HLA-A*33:60','HLA-A*33:61','HLA-A*33:62','HLA-A*33:63','HLA-A*33:64','HLA-A*33:65','HLA-A*33:66','HLA-A*33:67','HLA-A*33:68','HLA-A*33:69','HLA-A*33:70','HLA-A*33:71','HLA-A*33:72','HLA-A*33:75','HLA-A*33:76','HLA-A*33:77','HLA-A*33:78','HLA-A*33:79','HLA-A*33:81','HLA-A*33:82','HLA-A*33:83','HLA-A*33:84','HLA-A*33:85','HLA-A*33:86','HLA-A*33:87','HLA-A*33:88','HLA-A*33:89','HLA-A*33:90','HLA-A*33:91','HLA-A*33:92','HLA-A*33:93','HLA-A*33:94','HLA-A*33:95','HLA-A*33:97','HLA-A*33:98','HLA-A*33:99','HLA-A*34:01','HLA-A*34:02','HLA-A*34:03','HLA-A*34:04','HLA-A*34:05','HLA-A*34:06','HLA-A*34:07','HLA-A*34:08','HLA-A*34:09','HLA-A*34:11','HLA-A*34:12','HLA-A*34:13','HLA-A*34:14','HLA-A*34:15','HLA-A*34:16','HLA-A*34:17','HLA-A*34:18','HLA-A*34:19','HLA-A*34:20','HLA-A*34:21','HLA-A*36:01','HLA-A*36:02','HLA-A*36:03','HLA-A*36:04','HLA-A*36:05','HLA-A*36:06','HLA-A*36:07','HLA-A*36:08','HLA-A*43:01','HLA-A*66:01','HLA-A*66:02','HLA-A*66:03','HLA-A*66:04','HLA-A*66:05','HLA-A*66:06','HLA-A*66:07','HLA-A*66:08','HLA-A*66:09','HLA-A*66:10','HLA-A*66:11','HLA-A*66:12','HLA-A*66:13','HLA-A*66:14','HLA-A*66:15','HLA-A*66:16','HLA-A*66:17','HLA-A*66:18','HLA-A*66:19','HLA-A*66:20','HLA-A*66:21','HLA-A*66:22','HLA-A*66:23','HLA-A*66:24','HLA-A*66:25','HLA-A*66:29','HLA-A*66:30','HLA-A*66:31','HLA-A*66:32','HLA-A*68:01','HLA-A*68:02','HLA-A*68:03','HLA-A*68:04','HLA-A*68:05','HLA-A*68:06','HLA-A*68:07','HLA-A*68:08','HLA-A*68:09','HLA-A*68:10','HLA-A*68:100','HLA-A*68:101','HLA-A*68:102','HLA-A*68:103','HLA-A*68:104','HLA-A*68:105','HLA-A*68:106','HLA-A*68:107','HLA-A*68:108','HLA-A*68:109','HLA-A*68:110','HLA-A*68:111','HLA-A*68:112','HLA-A*68:113','HLA-A*68:114','HLA-A*68:115','HLA-A*68:116','HLA-A*68:117','HLA-A*68:118','HLA-A*68:119','HLA-A*68:12','HLA-A*68:121','HLA-A*68:122','HLA-A*68:123','HLA-A*68:124','HLA-A*68:125','HLA-A*68:126','HLA-A*68:127','HLA-A*68:128','HLA-A*68:129','HLA-A*68:13','HLA-A*68:130','HLA-A*68:131','HLA-A*68:132','HLA-A*68:133','HLA-A*68:134','HLA-A*68:135','HLA-A*68:136','HLA-A*68:137','HLA-A*68:138','HLA-A*68:139','HLA-A*68:14','HLA-A*68:140','HLA-A*68:141','HLA-A*68:143','HLA-A*68:144','HLA-A*68:145','HLA-A*68:146','HLA-A*68:147','HLA-A*68:149','HLA-A*68:15','HLA-A*68:150','HLA-A*68:151','HLA-A*68:152','HLA-A*68:153','HLA-A*68:154','HLA-A*68:155','HLA-A*68:156','HLA-A*68:157','HLA-A*68:158','HLA-A*68:16','HLA-A*68:160','HLA-A*68:161','HLA-A*68:162','HLA-A*68:163','HLA-A*68:164','HLA-A*68:165','HLA-A*68:166','HLA-A*68:167','HLA-A*68:168','HLA-A*68:169','HLA-A*68:17','HLA-A*68:170','HLA-A*68:172','HLA-A*68:173','HLA-A*68:174','HLA-A*68:175','HLA-A*68:176','HLA-A*68:177','HLA-A*68:178','HLA-A*68:179','HLA-A*68:180','HLA-A*68:183','HLA-A*68:184','HLA-A*68:185','HLA-A*68:186','HLA-A*68:187','HLA-A*68:188','HLA-A*68:189','HLA-A*68:19','HLA-A*68:190','HLA-A*68:191','HLA-A*68:192','HLA-A*68:193','HLA-A*68:194','HLA-A*68:195','HLA-A*68:196','HLA-A*68:197','HLA-A*68:198','HLA-A*68:20','HLA-A*68:200','HLA-A*68:201','HLA-A*68:202','HLA-A*68:204','HLA-A*68:21','HLA-A*68:22','HLA-A*68:23','HLA-A*68:24','HLA-A*68:25','HLA-A*68:26','HLA-A*68:27','HLA-A*68:28','HLA-A*68:29','HLA-A*68:30','HLA-A*68:31','HLA-A*68:32','HLA-A*68:33','HLA-A*68:34','HLA-A*68:35','HLA-A*68:36','HLA-A*68:37','HLA-A*68:38','HLA-A*68:39','HLA-A*68:40','HLA-A*68:41','HLA-A*68:42','HLA-A*68:43','HLA-A*68:44','HLA-A*68:45','HLA-A*68:46','HLA-A*68:47','HLA-A*68:48','HLA-A*68:50','HLA-A*68:51','HLA-A*68:52','HLA-A*68:53','HLA-A*68:54','HLA-A*68:55','HLA-A*68:56','HLA-A*68:57','HLA-A*68:58','HLA-A*68:60','HLA-A*68:61','HLA-A*68:62','HLA-A*68:63','HLA-A*68:64','HLA-A*68:65','HLA-A*68:66','HLA-A*68:67','HLA-A*68:68','HLA-A*68:69','HLA-A*68:70','HLA-A*68:71','HLA-A*68:72','HLA-A*68:73','HLA-A*68:74','HLA-A*68:75','HLA-A*68:76','HLA-A*68:77','HLA-A*68:78','HLA-A*68:79','HLA-A*68:80','HLA-A*68:81','HLA-A*68:82','HLA-A*68:83','HLA-A*68:84','HLA-A*68:85','HLA-A*68:86','HLA-A*68:87','HLA-A*68:88','HLA-A*68:89','HLA-A*68:90','HLA-A*68:91','HLA-A*68:92','HLA-A*68:93','HLA-A*68:95','HLA-A*68:96','HLA-A*68:97','HLA-A*68:98','HLA-A*68:99','HLA-A*69:01','HLA-A*69:02','HLA-A*69:03','HLA-A*69:04','HLA-A*69:05','HLA-A*74:01','HLA-A*74:02','HLA-A*74:03','HLA-A*74:04','HLA-A*74:05','HLA-A*74:06','HLA-A*74:07','HLA-A*74:08','HLA-A*74:09','HLA-A*74:10','HLA-A*74:11','HLA-A*74:13','HLA-A*74:15','HLA-A*74:16','HLA-A*74:17','HLA-A*74:18','HLA-A*74:19','HLA-A*74:20','HLA-A*74:21','HLA-A*74:22','HLA-A*74:23','HLA-A*74:24','HLA-A*74:25','HLA-A*74:26','HLA-A*74:27','HLA-A*74:28','HLA-A*74:29','HLA-A*74:30','HLA-A*74:31','HLA-A*74:33','HLA-A*74:34','HLA-A*80:01','HLA-A*80:02','HLA-A*80:03','HLA-A*80:04','HLA-B*07:02','HLA-B*07:03','HLA-B*07:04','HLA-B*07:05','HLA-B*07:06','HLA-B*07:07','HLA-B*07:08','HLA-B*07:09','HLA-B*07:10','HLA-B*07:100','HLA-B*07:101','HLA-B*07:102','HLA-B*07:103','HLA-B*07:104','HLA-B*07:105','HLA-B*07:106','HLA-B*07:107','HLA-B*07:108','HLA-B*07:109','HLA-B*07:11','HLA-B*07:110','HLA-B*07:112','HLA-B*07:113','HLA-B*07:114','HLA-B*07:115','HLA-B*07:116','HLA-B*07:117','HLA-B*07:118','HLA-B*07:119','HLA-B*07:12','HLA-B*07:120','HLA-B*07:121','HLA-B*07:122','HLA-B*07:123','HLA-B*07:124','HLA-B*07:125','HLA-B*07:126','HLA-B*07:127','HLA-B*07:128','HLA-B*07:129','HLA-B*07:13','HLA-B*07:130','HLA-B*07:131','HLA-B*07:132','HLA-B*07:133','HLA-B*07:134','HLA-B*07:136','HLA-B*07:137','HLA-B*07:138','HLA-B*07:139','HLA-B*07:14','HLA-B*07:140','HLA-B*07:141','HLA-B*07:142','HLA-B*07:143','HLA-B*07:144','HLA-B*07:145','HLA-B*07:146','HLA-B*07:147','HLA-B*07:148','HLA-B*07:149','HLA-B*07:15','HLA-B*07:150','HLA-B*07:151','HLA-B*07:152','HLA-B*07:153','HLA-B*07:154','HLA-B*07:155','HLA-B*07:156','HLA-B*07:157','HLA-B*07:158','HLA-B*07:159','HLA-B*07:16','HLA-B*07:160','HLA-B*07:162','HLA-B*07:163','HLA-B*07:164','HLA-B*07:165','HLA-B*07:166','HLA-B*07:168','HLA-B*07:169','HLA-B*07:17','HLA-B*07:170','HLA-B*07:171','HLA-B*07:172','HLA-B*07:173','HLA-B*07:174','HLA-B*07:175','HLA-B*07:176','HLA-B*07:177','HLA-B*07:178','HLA-B*07:179','HLA-B*07:18','HLA-B*07:180','HLA-B*07:183','HLA-B*07:184','HLA-B*07:185','HLA-B*07:186','HLA-B*07:187','HLA-B*07:188','HLA-B*07:189','HLA-B*07:19','HLA-B*07:190','HLA-B*07:191','HLA-B*07:192','HLA-B*07:193','HLA-B*07:194','HLA-B*07:195','HLA-B*07:196','HLA-B*07:197','HLA-B*07:198','HLA-B*07:199','HLA-B*07:20','HLA-B*07:200','HLA-B*07:202','HLA-B*07:203','HLA-B*07:204','HLA-B*07:205','HLA-B*07:206','HLA-B*07:207','HLA-B*07:208','HLA-B*07:209','HLA-B*07:21','HLA-B*07:210','HLA-B*07:211','HLA-B*07:212','HLA-B*07:213','HLA-B*07:214','HLA-B*07:215','HLA-B*07:216','HLA-B*07:217','HLA-B*07:218','HLA-B*07:219','HLA-B*07:22','HLA-B*07:220','HLA-B*07:221','HLA-B*07:222','HLA-B*07:223','HLA-B*07:224','HLA-B*07:225','HLA-B*07:226','HLA-B*07:227','HLA-B*07:228','HLA-B*07:229','HLA-B*07:23','HLA-B*07:230','HLA-B*07:232','HLA-B*07:233','HLA-B*07:234','HLA-B*07:235','HLA-B*07:236','HLA-B*07:237','HLA-B*07:238','HLA-B*07:239','HLA-B*07:24','HLA-B*07:240','HLA-B*07:241','HLA-B*07:242','HLA-B*07:243','HLA-B*07:244','HLA-B*07:245','HLA-B*07:246','HLA-B*07:247','HLA-B*07:248','HLA-B*07:249','HLA-B*07:25','HLA-B*07:250','HLA-B*07:252','HLA-B*07:253','HLA-B*07:254','HLA-B*07:255','HLA-B*07:256','HLA-B*07:257','HLA-B*07:258','HLA-B*07:259','HLA-B*07:26','HLA-B*07:260','HLA-B*07:261','HLA-B*07:262','HLA-B*07:263','HLA-B*07:264','HLA-B*07:265','HLA-B*07:266','HLA-B*07:267','HLA-B*07:268','HLA-B*07:269','HLA-B*07:27','HLA-B*07:270','HLA-B*07:271','HLA-B*07:273','HLA-B*07:274','HLA-B*07:275','HLA-B*07:276','HLA-B*07:277','HLA-B*07:278','HLA-B*07:279','HLA-B*07:28','HLA-B*07:280','HLA-B*07:281','HLA-B*07:282','HLA-B*07:283','HLA-B*07:284','HLA-B*07:286','HLA-B*07:287','HLA-B*07:288','HLA-B*07:289','HLA-B*07:29','HLA-B*07:290','HLA-B*07:291','HLA-B*07:292','HLA-B*07:293','HLA-B*07:294','HLA-B*07:295','HLA-B*07:296','HLA-B*07:297','HLA-B*07:298','HLA-B*07:299','HLA-B*07:30','HLA-B*07:300','HLA-B*07:301','HLA-B*07:302','HLA-B*07:303','HLA-B*07:304','HLA-B*07:305','HLA-B*07:306','HLA-B*07:307','HLA-B*07:308','HLA-B*07:309','HLA-B*07:31','HLA-B*07:310','HLA-B*07:311','HLA-B*07:312','HLA-B*07:313','HLA-B*07:314','HLA-B*07:317','HLA-B*07:319','HLA-B*07:32','HLA-B*07:320','HLA-B*07:321','HLA-B*07:322','HLA-B*07:323','HLA-B*07:324','HLA-B*07:326','HLA-B*07:327','HLA-B*07:328','HLA-B*07:329','HLA-B*07:33','HLA-B*07:331','HLA-B*07:332','HLA-B*07:333','HLA-B*07:334','HLA-B*07:335','HLA-B*07:336','HLA-B*07:337','HLA-B*07:338','HLA-B*07:339','HLA-B*07:34','HLA-B*07:340','HLA-B*07:341','HLA-B*07:342','HLA-B*07:344','HLA-B*07:345','HLA-B*07:346','HLA-B*07:347','HLA-B*07:348','HLA-B*07:349','HLA-B*07:35','HLA-B*07:350','HLA-B*07:352','HLA-B*07:353','HLA-B*07:354','HLA-B*07:355','HLA-B*07:356','HLA-B*07:357','HLA-B*07:358','HLA-B*07:36','HLA-B*07:37','HLA-B*07:38','HLA-B*07:39','HLA-B*07:40','HLA-B*07:41','HLA-B*07:42','HLA-B*07:43','HLA-B*07:44','HLA-B*07:45','HLA-B*07:46','HLA-B*07:47','HLA-B*07:48','HLA-B*07:50','HLA-B*07:51','HLA-B*07:52','HLA-B*07:53','HLA-B*07:54','HLA-B*07:55','HLA-B*07:56','HLA-B*07:57','HLA-B*07:58','HLA-B*07:59','HLA-B*07:60','HLA-B*07:61','HLA-B*07:62','HLA-B*07:63','HLA-B*07:64','HLA-B*07:65','HLA-B*07:66','HLA-B*07:68','HLA-B*07:69','HLA-B*07:70','HLA-B*07:71','HLA-B*07:72','HLA-B*07:73','HLA-B*07:74','HLA-B*07:75','HLA-B*07:76','HLA-B*07:77','HLA-B*07:78','HLA-B*07:79','HLA-B*07:80','HLA-B*07:81','HLA-B*07:82','HLA-B*07:83','HLA-B*07:84','HLA-B*07:85','HLA-B*07:86','HLA-B*07:87','HLA-B*07:88','HLA-B*07:89','HLA-B*07:90','HLA-B*07:91','HLA-B*07:92','HLA-B*07:93','HLA-B*07:94','HLA-B*07:95','HLA-B*07:96','HLA-B*07:97','HLA-B*07:98','HLA-B*07:99','HLA-B*08:01','HLA-B*08:02','HLA-B*08:03','HLA-B*08:04','HLA-B*08:05','HLA-B*08:07','HLA-B*08:09','HLA-B*08:10','HLA-B*08:100','HLA-B*08:101','HLA-B*08:102','HLA-B*08:103','HLA-B*08:104','HLA-B*08:105','HLA-B*08:106','HLA-B*08:107','HLA-B*08:108','HLA-B*08:109','HLA-B*08:11','HLA-B*08:110','HLA-B*08:111','HLA-B*08:112','HLA-B*08:113','HLA-B*08:114','HLA-B*08:115','HLA-B*08:116','HLA-B*08:117','HLA-B*08:118','HLA-B*08:119','HLA-B*08:12','HLA-B*08:120','HLA-B*08:121','HLA-B*08:122','HLA-B*08:123','HLA-B*08:124','HLA-B*08:125','HLA-B*08:126','HLA-B*08:127','HLA-B*08:128','HLA-B*08:129','HLA-B*08:13','HLA-B*08:130','HLA-B*08:131','HLA-B*08:132','HLA-B*08:133','HLA-B*08:134','HLA-B*08:135','HLA-B*08:136','HLA-B*08:137','HLA-B*08:138','HLA-B*08:139','HLA-B*08:14','HLA-B*08:140','HLA-B*08:141','HLA-B*08:142','HLA-B*08:143','HLA-B*08:144','HLA-B*08:145','HLA-B*08:146','HLA-B*08:147','HLA-B*08:149','HLA-B*08:15','HLA-B*08:150','HLA-B*08:151','HLA-B*08:152','HLA-B*08:153','HLA-B*08:154','HLA-B*08:155','HLA-B*08:156','HLA-B*08:157','HLA-B*08:158','HLA-B*08:159','HLA-B*08:16','HLA-B*08:160','HLA-B*08:161','HLA-B*08:162','HLA-B*08:163','HLA-B*08:164','HLA-B*08:165','HLA-B*08:166','HLA-B*08:167','HLA-B*08:168','HLA-B*08:169','HLA-B*08:17','HLA-B*08:170','HLA-B*08:171','HLA-B*08:172','HLA-B*08:173','HLA-B*08:174','HLA-B*08:175','HLA-B*08:176','HLA-B*08:177','HLA-B*08:178','HLA-B*08:179','HLA-B*08:18','HLA-B*08:180','HLA-B*08:181','HLA-B*08:182','HLA-B*08:183','HLA-B*08:184','HLA-B*08:185','HLA-B*08:186','HLA-B*08:187','HLA-B*08:188','HLA-B*08:189','HLA-B*08:190','HLA-B*08:191','HLA-B*08:192','HLA-B*08:193','HLA-B*08:194','HLA-B*08:195','HLA-B*08:196','HLA-B*08:197','HLA-B*08:198','HLA-B*08:199','HLA-B*08:20','HLA-B*08:200','HLA-B*08:201','HLA-B*08:202','HLA-B*08:203','HLA-B*08:204','HLA-B*08:205','HLA-B*08:206','HLA-B*08:207','HLA-B*08:208','HLA-B*08:209','HLA-B*08:21','HLA-B*08:210','HLA-B*08:211','HLA-B*08:212','HLA-B*08:213','HLA-B*08:216','HLA-B*08:217','HLA-B*08:218','HLA-B*08:219','HLA-B*08:22','HLA-B*08:221','HLA-B*08:222','HLA-B*08:223','HLA-B*08:224','HLA-B*08:23','HLA-B*08:24','HLA-B*08:25','HLA-B*08:26','HLA-B*08:27','HLA-B*08:28','HLA-B*08:29','HLA-B*08:31','HLA-B*08:32','HLA-B*08:33','HLA-B*08:34','HLA-B*08:35','HLA-B*08:36','HLA-B*08:37','HLA-B*08:38','HLA-B*08:39','HLA-B*08:40','HLA-B*08:41','HLA-B*08:42','HLA-B*08:43','HLA-B*08:44','HLA-B*08:45','HLA-B*08:46','HLA-B*08:47','HLA-B*08:48','HLA-B*08:49','HLA-B*08:50','HLA-B*08:51','HLA-B*08:52','HLA-B*08:53','HLA-B*08:54','HLA-B*08:55','HLA-B*08:56','HLA-B*08:57','HLA-B*08:58','HLA-B*08:59','HLA-B*08:60','HLA-B*08:61','HLA-B*08:62','HLA-B*08:63','HLA-B*08:64','HLA-B*08:65','HLA-B*08:66','HLA-B*08:68','HLA-B*08:69','HLA-B*08:70','HLA-B*08:71','HLA-B*08:73','HLA-B*08:74','HLA-B*08:75','HLA-B*08:76','HLA-B*08:77','HLA-B*08:78','HLA-B*08:79','HLA-B*08:80','HLA-B*08:81','HLA-B*08:83','HLA-B*08:84','HLA-B*08:85','HLA-B*08:87','HLA-B*08:88','HLA-B*08:89','HLA-B*08:90','HLA-B*08:91','HLA-B*08:92','HLA-B*08:93','HLA-B*08:94','HLA-B*08:95','HLA-B*08:96','HLA-B*08:97','HLA-B*08:98','HLA-B*08:99','HLA-B*13:01','HLA-B*13:02','HLA-B*13:03','HLA-B*13:04','HLA-B*13:06','HLA-B*13:08','HLA-B*13:09','HLA-B*13:10','HLA-B*13:100','HLA-B*13:101','HLA-B*13:102','HLA-B*13:104','HLA-B*13:105','HLA-B*13:106','HLA-B*13:107','HLA-B*13:108','HLA-B*13:109','HLA-B*13:11','HLA-B*13:110','HLA-B*13:111','HLA-B*13:112','HLA-B*13:113','HLA-B*13:114','HLA-B*13:115','HLA-B*13:117','HLA-B*13:118','HLA-B*13:119','HLA-B*13:12','HLA-B*13:120','HLA-B*13:121','HLA-B*13:122','HLA-B*13:124','HLA-B*13:125','HLA-B*13:126','HLA-B*13:127','HLA-B*13:128','HLA-B*13:129','HLA-B*13:13','HLA-B*13:130','HLA-B*13:14','HLA-B*13:15','HLA-B*13:16','HLA-B*13:17','HLA-B*13:18','HLA-B*13:19','HLA-B*13:20','HLA-B*13:21','HLA-B*13:22','HLA-B*13:23','HLA-B*13:25','HLA-B*13:26','HLA-B*13:27','HLA-B*13:28','HLA-B*13:29','HLA-B*13:30','HLA-B*13:31','HLA-B*13:32','HLA-B*13:33','HLA-B*13:34','HLA-B*13:35','HLA-B*13:36','HLA-B*13:37','HLA-B*13:38','HLA-B*13:39','HLA-B*13:40','HLA-B*13:41','HLA-B*13:42','HLA-B*13:43','HLA-B*13:44','HLA-B*13:45','HLA-B*13:46','HLA-B*13:47','HLA-B*13:48','HLA-B*13:50','HLA-B*13:51','HLA-B*13:52','HLA-B*13:53','HLA-B*13:54','HLA-B*13:55','HLA-B*13:57','HLA-B*13:58','HLA-B*13:59','HLA-B*13:60','HLA-B*13:61','HLA-B*13:62','HLA-B*13:64','HLA-B*13:65','HLA-B*13:66','HLA-B*13:67','HLA-B*13:68','HLA-B*13:69','HLA-B*13:70','HLA-B*13:71','HLA-B*13:72','HLA-B*13:73','HLA-B*13:74','HLA-B*13:75','HLA-B*13:77','HLA-B*13:78','HLA-B*13:79','HLA-B*13:80','HLA-B*13:81','HLA-B*13:82','HLA-B*13:83','HLA-B*13:84','HLA-B*13:85','HLA-B*13:86','HLA-B*13:87','HLA-B*13:88','HLA-B*13:89','HLA-B*13:90','HLA-B*13:91','HLA-B*13:92','HLA-B*13:93','HLA-B*13:94','HLA-B*13:95','HLA-B*13:96','HLA-B*13:97','HLA-B*13:98','HLA-B*13:99','HLA-B*14:01','HLA-B*14:02','HLA-B*14:03','HLA-B*14:04','HLA-B*14:05','HLA-B*14:06','HLA-B*14:08','HLA-B*14:09','HLA-B*14:10','HLA-B*14:11','HLA-B*14:12','HLA-B*14:13','HLA-B*14:14','HLA-B*14:15','HLA-B*14:16','HLA-B*14:17','HLA-B*14:18','HLA-B*14:19','HLA-B*14:20','HLA-B*14:21','HLA-B*14:22','HLA-B*14:23','HLA-B*14:24','HLA-B*14:25','HLA-B*14:26','HLA-B*14:27','HLA-B*14:28','HLA-B*14:29','HLA-B*14:30','HLA-B*14:31','HLA-B*14:32','HLA-B*14:33','HLA-B*14:34','HLA-B*14:35','HLA-B*14:36','HLA-B*14:37','HLA-B*14:38','HLA-B*14:39','HLA-B*14:40','HLA-B*14:42','HLA-B*14:43','HLA-B*14:44','HLA-B*14:45','HLA-B*14:46','HLA-B*14:47','HLA-B*14:48','HLA-B*14:49','HLA-B*14:50','HLA-B*14:51','HLA-B*14:52','HLA-B*14:53','HLA-B*14:54','HLA-B*14:55','HLA-B*14:56','HLA-B*14:57','HLA-B*14:58','HLA-B*14:59','HLA-B*14:60','HLA-B*14:61','HLA-B*14:62','HLA-B*14:63','HLA-B*14:64','HLA-B*14:65','HLA-B*14:66','HLA-B*14:67','HLA-B*14:68','HLA-B*15:01','HLA-B*15:02','HLA-B*15:03','HLA-B*15:04','HLA-B*15:05','HLA-B*15:06','HLA-B*15:07','HLA-B*15:08','HLA-B*15:09','HLA-B*15:10','HLA-B*15:101','HLA-B*15:102','HLA-B*15:103','HLA-B*15:104','HLA-B*15:105','HLA-B*15:106','HLA-B*15:107','HLA-B*15:108','HLA-B*15:109','HLA-B*15:11','HLA-B*15:110','HLA-B*15:112','HLA-B*15:113','HLA-B*15:114','HLA-B*15:115','HLA-B*15:116','HLA-B*15:117','HLA-B*15:118','HLA-B*15:119','HLA-B*15:12','HLA-B*15:120','HLA-B*15:121','HLA-B*15:122','HLA-B*15:123','HLA-B*15:124','HLA-B*15:125','HLA-B*15:126','HLA-B*15:127','HLA-B*15:128','HLA-B*15:129','HLA-B*15:13','HLA-B*15:131','HLA-B*15:132','HLA-B*15:133','HLA-B*15:134','HLA-B*15:135','HLA-B*15:136','HLA-B*15:137','HLA-B*15:138','HLA-B*15:139','HLA-B*15:14','HLA-B*15:140','HLA-B*15:141','HLA-B*15:142','HLA-B*15:143','HLA-B*15:144','HLA-B*15:145','HLA-B*15:146','HLA-B*15:147','HLA-B*15:148','HLA-B*15:15','HLA-B*15:150','HLA-B*15:151','HLA-B*15:152','HLA-B*15:153','HLA-B*15:154','HLA-B*15:155','HLA-B*15:156','HLA-B*15:157','HLA-B*15:158','HLA-B*15:159','HLA-B*15:16','HLA-B*15:160','HLA-B*15:161','HLA-B*15:162','HLA-B*15:163','HLA-B*15:164','HLA-B*15:165','HLA-B*15:166','HLA-B*15:167','HLA-B*15:168','HLA-B*15:169','HLA-B*15:17','HLA-B*15:170','HLA-B*15:171','HLA-B*15:172','HLA-B*15:173','HLA-B*15:174','HLA-B*15:175','HLA-B*15:176','HLA-B*15:177','HLA-B*15:178','HLA-B*15:179','HLA-B*15:18','HLA-B*15:180','HLA-B*15:183','HLA-B*15:184','HLA-B*15:185','HLA-B*15:186','HLA-B*15:187','HLA-B*15:188','HLA-B*15:189','HLA-B*15:19','HLA-B*15:191','HLA-B*15:192','HLA-B*15:193','HLA-B*15:194','HLA-B*15:195','HLA-B*15:196','HLA-B*15:197','HLA-B*15:198','HLA-B*15:199','HLA-B*15:20','HLA-B*15:200','HLA-B*15:201','HLA-B*15:202','HLA-B*15:203','HLA-B*15:204','HLA-B*15:205','HLA-B*15:206','HLA-B*15:207','HLA-B*15:208','HLA-B*15:21','HLA-B*15:210','HLA-B*15:211','HLA-B*15:212','HLA-B*15:213','HLA-B*15:214','HLA-B*15:215','HLA-B*15:216','HLA-B*15:217','HLA-B*15:219','HLA-B*15:220','HLA-B*15:221','HLA-B*15:222','HLA-B*15:223','HLA-B*15:224','HLA-B*15:225','HLA-B*15:227','HLA-B*15:228','HLA-B*15:229','HLA-B*15:23','HLA-B*15:230','HLA-B*15:231','HLA-B*15:232','HLA-B*15:233','HLA-B*15:234','HLA-B*15:235','HLA-B*15:236','HLA-B*15:237','HLA-B*15:238','HLA-B*15:239','HLA-B*15:24','HLA-B*15:240','HLA-B*15:241','HLA-B*15:242','HLA-B*15:243','HLA-B*15:244','HLA-B*15:247','HLA-B*15:248','HLA-B*15:249','HLA-B*15:25','HLA-B*15:250','HLA-B*15:251','HLA-B*15:252','HLA-B*15:253','HLA-B*15:254','HLA-B*15:255','HLA-B*15:256','HLA-B*15:257','HLA-B*15:259','HLA-B*15:260','HLA-B*15:261','HLA-B*15:263','HLA-B*15:264','HLA-B*15:265','HLA-B*15:266','HLA-B*15:267','HLA-B*15:268','HLA-B*15:269','HLA-B*15:27','HLA-B*15:270','HLA-B*15:271','HLA-B*15:273','HLA-B*15:274','HLA-B*15:275','HLA-B*15:276','HLA-B*15:277','HLA-B*15:278','HLA-B*15:279','HLA-B*15:28','HLA-B*15:280','HLA-B*15:281','HLA-B*15:282','HLA-B*15:283','HLA-B*15:284','HLA-B*15:285','HLA-B*15:286','HLA-B*15:287','HLA-B*15:288','HLA-B*15:289','HLA-B*15:29','HLA-B*15:290','HLA-B*15:291','HLA-B*15:292','HLA-B*15:293','HLA-B*15:295','HLA-B*15:296','HLA-B*15:297','HLA-B*15:298','HLA-B*15:299','HLA-B*15:30','HLA-B*15:300','HLA-B*15:301','HLA-B*15:303','HLA-B*15:305','HLA-B*15:306','HLA-B*15:307','HLA-B*15:308','HLA-B*15:309','HLA-B*15:31','HLA-B*15:310','HLA-B*15:311','HLA-B*15:312','HLA-B*15:313','HLA-B*15:314','HLA-B*15:315','HLA-B*15:316','HLA-B*15:317','HLA-B*15:318','HLA-B*15:319','HLA-B*15:32','HLA-B*15:320','HLA-B*15:322','HLA-B*15:323','HLA-B*15:324','HLA-B*15:325','HLA-B*15:326','HLA-B*15:327','HLA-B*15:328','HLA-B*15:329','HLA-B*15:33','HLA-B*15:330','HLA-B*15:331','HLA-B*15:332','HLA-B*15:333','HLA-B*15:334','HLA-B*15:335','HLA-B*15:336','HLA-B*15:337','HLA-B*15:338','HLA-B*15:339','HLA-B*15:34','HLA-B*15:340','HLA-B*15:341','HLA-B*15:342','HLA-B*15:343','HLA-B*15:344','HLA-B*15:345','HLA-B*15:346','HLA-B*15:347','HLA-B*15:348','HLA-B*15:349','HLA-B*15:35','HLA-B*15:350','HLA-B*15:351','HLA-B*15:352','HLA-B*15:353','HLA-B*15:354','HLA-B*15:355','HLA-B*15:356','HLA-B*15:357','HLA-B*15:358','HLA-B*15:359','HLA-B*15:36','HLA-B*15:360','HLA-B*15:361','HLA-B*15:362','HLA-B*15:363','HLA-B*15:364','HLA-B*15:365','HLA-B*15:366','HLA-B*15:367','HLA-B*15:368','HLA-B*15:369','HLA-B*15:37','HLA-B*15:370','HLA-B*15:371','HLA-B*15:372','HLA-B*15:373','HLA-B*15:374','HLA-B*15:376','HLA-B*15:378','HLA-B*15:379','HLA-B*15:38','HLA-B*15:381','HLA-B*15:382','HLA-B*15:383','HLA-B*15:384','HLA-B*15:385','HLA-B*15:386','HLA-B*15:387','HLA-B*15:388','HLA-B*15:389','HLA-B*15:39','HLA-B*15:390','HLA-B*15:391','HLA-B*15:392','HLA-B*15:393','HLA-B*15:394','HLA-B*15:395','HLA-B*15:396','HLA-B*15:397','HLA-B*15:398','HLA-B*15:399','HLA-B*15:40','HLA-B*15:401','HLA-B*15:402','HLA-B*15:403','HLA-B*15:404','HLA-B*15:405','HLA-B*15:406','HLA-B*15:407','HLA-B*15:408','HLA-B*15:409','HLA-B*15:410','HLA-B*15:411','HLA-B*15:412','HLA-B*15:413','HLA-B*15:414','HLA-B*15:415','HLA-B*15:416','HLA-B*15:417','HLA-B*15:418','HLA-B*15:419','HLA-B*15:42','HLA-B*15:420','HLA-B*15:421','HLA-B*15:422','HLA-B*15:423','HLA-B*15:424','HLA-B*15:425','HLA-B*15:426','HLA-B*15:427','HLA-B*15:428','HLA-B*15:429','HLA-B*15:43','HLA-B*15:430','HLA-B*15:431','HLA-B*15:432','HLA-B*15:433','HLA-B*15:434','HLA-B*15:435','HLA-B*15:436','HLA-B*15:437','HLA-B*15:438','HLA-B*15:439','HLA-B*15:44','HLA-B*15:440','HLA-B*15:441','HLA-B*15:442','HLA-B*15:443','HLA-B*15:444','HLA-B*15:445','HLA-B*15:446','HLA-B*15:447','HLA-B*15:448','HLA-B*15:449','HLA-B*15:45','HLA-B*15:450','HLA-B*15:451','HLA-B*15:452','HLA-B*15:453','HLA-B*15:455','HLA-B*15:456','HLA-B*15:457','HLA-B*15:458','HLA-B*15:459','HLA-B*15:46','HLA-B*15:460','HLA-B*15:461','HLA-B*15:462','HLA-B*15:464','HLA-B*15:465','HLA-B*15:466','HLA-B*15:467','HLA-B*15:468','HLA-B*15:469','HLA-B*15:47','HLA-B*15:470','HLA-B*15:471','HLA-B*15:472','HLA-B*15:473','HLA-B*15:474','HLA-B*15:475','HLA-B*15:476','HLA-B*15:477','HLA-B*15:478','HLA-B*15:479','HLA-B*15:48','HLA-B*15:480','HLA-B*15:481','HLA-B*15:482','HLA-B*15:484','HLA-B*15:485','HLA-B*15:486','HLA-B*15:488','HLA-B*15:489','HLA-B*15:49','HLA-B*15:490','HLA-B*15:491','HLA-B*15:492','HLA-B*15:493','HLA-B*15:494','HLA-B*15:495','HLA-B*15:497','HLA-B*15:498','HLA-B*15:50','HLA-B*15:51','HLA-B*15:52','HLA-B*15:53','HLA-B*15:54','HLA-B*15:55','HLA-B*15:56','HLA-B*15:57','HLA-B*15:58','HLA-B*15:60','HLA-B*15:61','HLA-B*15:62','HLA-B*15:63','HLA-B*15:64','HLA-B*15:65','HLA-B*15:66','HLA-B*15:67','HLA-B*15:68','HLA-B*15:69','HLA-B*15:70','HLA-B*15:71','HLA-B*15:72','HLA-B*15:73','HLA-B*15:74','HLA-B*15:75','HLA-B*15:76','HLA-B*15:77','HLA-B*15:78','HLA-B*15:80','HLA-B*15:81','HLA-B*15:82','HLA-B*15:83','HLA-B*15:84','HLA-B*15:85','HLA-B*15:86','HLA-B*15:87','HLA-B*15:88','HLA-B*15:89','HLA-B*15:90','HLA-B*15:91','HLA-B*15:92','HLA-B*15:93','HLA-B*15:95','HLA-B*15:96','HLA-B*15:97','HLA-B*15:98','HLA-B*15:99','HLA-B*18:01','HLA-B*18:02','HLA-B*18:03','HLA-B*18:04','HLA-B*18:05','HLA-B*18:06','HLA-B*18:07','HLA-B*18:08','HLA-B*18:09','HLA-B*18:10','HLA-B*18:100','HLA-B*18:101','HLA-B*18:102','HLA-B*18:103','HLA-B*18:104','HLA-B*18:105','HLA-B*18:106','HLA-B*18:107','HLA-B*18:108','HLA-B*18:109','HLA-B*18:11','HLA-B*18:110','HLA-B*18:111','HLA-B*18:112','HLA-B*18:113','HLA-B*18:114','HLA-B*18:115','HLA-B*18:116','HLA-B*18:117','HLA-B*18:118','HLA-B*18:119','HLA-B*18:12','HLA-B*18:120','HLA-B*18:121','HLA-B*18:122','HLA-B*18:123','HLA-B*18:124','HLA-B*18:125','HLA-B*18:126','HLA-B*18:127','HLA-B*18:128','HLA-B*18:129','HLA-B*18:13','HLA-B*18:130','HLA-B*18:131','HLA-B*18:132','HLA-B*18:133','HLA-B*18:134','HLA-B*18:135','HLA-B*18:136','HLA-B*18:137','HLA-B*18:139','HLA-B*18:14','HLA-B*18:140','HLA-B*18:141','HLA-B*18:142','HLA-B*18:143','HLA-B*18:144','HLA-B*18:145','HLA-B*18:146','HLA-B*18:147','HLA-B*18:148','HLA-B*18:149','HLA-B*18:15','HLA-B*18:150','HLA-B*18:151','HLA-B*18:152','HLA-B*18:153','HLA-B*18:155','HLA-B*18:156','HLA-B*18:157','HLA-B*18:158','HLA-B*18:159','HLA-B*18:160','HLA-B*18:161','HLA-B*18:18','HLA-B*18:19','HLA-B*18:20','HLA-B*18:21','HLA-B*18:22','HLA-B*18:24','HLA-B*18:25','HLA-B*18:26','HLA-B*18:27','HLA-B*18:28','HLA-B*18:29','HLA-B*18:30','HLA-B*18:31','HLA-B*18:32','HLA-B*18:33','HLA-B*18:34','HLA-B*18:35','HLA-B*18:36','HLA-B*18:37','HLA-B*18:38','HLA-B*18:39','HLA-B*18:40','HLA-B*18:41','HLA-B*18:42','HLA-B*18:43','HLA-B*18:44','HLA-B*18:45','HLA-B*18:46','HLA-B*18:47','HLA-B*18:48','HLA-B*18:49','HLA-B*18:50','HLA-B*18:51','HLA-B*18:52','HLA-B*18:53','HLA-B*18:54','HLA-B*18:55','HLA-B*18:56','HLA-B*18:57','HLA-B*18:58','HLA-B*18:59','HLA-B*18:60','HLA-B*18:61','HLA-B*18:62','HLA-B*18:63','HLA-B*18:64','HLA-B*18:65','HLA-B*18:66','HLA-B*18:67','HLA-B*18:68','HLA-B*18:69','HLA-B*18:70','HLA-B*18:71','HLA-B*18:72','HLA-B*18:73','HLA-B*18:75','HLA-B*18:76','HLA-B*18:77','HLA-B*18:78','HLA-B*18:79','HLA-B*18:80','HLA-B*18:81','HLA-B*18:82','HLA-B*18:83','HLA-B*18:84','HLA-B*18:85','HLA-B*18:86','HLA-B*18:87','HLA-B*18:88','HLA-B*18:89','HLA-B*18:90','HLA-B*18:91','HLA-B*18:92','HLA-B*18:93','HLA-B*18:95','HLA-B*18:96','HLA-B*18:97','HLA-B*18:98','HLA-B*18:99','HLA-B*27:01','HLA-B*27:02','HLA-B*27:03','HLA-B*27:04','HLA-B*27:05','HLA-B*27:06','HLA-B*27:07','HLA-B*27:08','HLA-B*27:09','HLA-B*27:10','HLA-B*27:100','HLA-B*27:101','HLA-B*27:102','HLA-B*27:103','HLA-B*27:104','HLA-B*27:105','HLA-B*27:106','HLA-B*27:107','HLA-B*27:108','HLA-B*27:109','HLA-B*27:11','HLA-B*27:110','HLA-B*27:111','HLA-B*27:112','HLA-B*27:113','HLA-B*27:114','HLA-B*27:115','HLA-B*27:116','HLA-B*27:117','HLA-B*27:118','HLA-B*27:119','HLA-B*27:12','HLA-B*27:120','HLA-B*27:121','HLA-B*27:122','HLA-B*27:123','HLA-B*27:124','HLA-B*27:125','HLA-B*27:126','HLA-B*27:127','HLA-B*27:128','HLA-B*27:129','HLA-B*27:13','HLA-B*27:130','HLA-B*27:131','HLA-B*27:132','HLA-B*27:133','HLA-B*27:134','HLA-B*27:135','HLA-B*27:136','HLA-B*27:137','HLA-B*27:138','HLA-B*27:139','HLA-B*27:14','HLA-B*27:140','HLA-B*27:141','HLA-B*27:142','HLA-B*27:143','HLA-B*27:144','HLA-B*27:145','HLA-B*27:146','HLA-B*27:147','HLA-B*27:148','HLA-B*27:149','HLA-B*27:15','HLA-B*27:150','HLA-B*27:151','HLA-B*27:152','HLA-B*27:153','HLA-B*27:154','HLA-B*27:155','HLA-B*27:156','HLA-B*27:157','HLA-B*27:158','HLA-B*27:159','HLA-B*27:16','HLA-B*27:160','HLA-B*27:161','HLA-B*27:162','HLA-B*27:163','HLA-B*27:164','HLA-B*27:165','HLA-B*27:166','HLA-B*27:167','HLA-B*27:168','HLA-B*27:169','HLA-B*27:17','HLA-B*27:170','HLA-B*27:171','HLA-B*27:172','HLA-B*27:173','HLA-B*27:174','HLA-B*27:175','HLA-B*27:177','HLA-B*27:178','HLA-B*27:179','HLA-B*27:18','HLA-B*27:180','HLA-B*27:181','HLA-B*27:182','HLA-B*27:183','HLA-B*27:184','HLA-B*27:185','HLA-B*27:186','HLA-B*27:187','HLA-B*27:188','HLA-B*27:19','HLA-B*27:20','HLA-B*27:21','HLA-B*27:23','HLA-B*27:24','HLA-B*27:25','HLA-B*27:26','HLA-B*27:27','HLA-B*27:28','HLA-B*27:29','HLA-B*27:30','HLA-B*27:31','HLA-B*27:32','HLA-B*27:33','HLA-B*27:34','HLA-B*27:35','HLA-B*27:36','HLA-B*27:37','HLA-B*27:38','HLA-B*27:39','HLA-B*27:40','HLA-B*27:41','HLA-B*27:42','HLA-B*27:43','HLA-B*27:44','HLA-B*27:45','HLA-B*27:46','HLA-B*27:47','HLA-B*27:48','HLA-B*27:49','HLA-B*27:50','HLA-B*27:51','HLA-B*27:52','HLA-B*27:53','HLA-B*27:54','HLA-B*27:55','HLA-B*27:56','HLA-B*27:57','HLA-B*27:58','HLA-B*27:60','HLA-B*27:61','HLA-B*27:62','HLA-B*27:63','HLA-B*27:67','HLA-B*27:68','HLA-B*27:69','HLA-B*27:70','HLA-B*27:71','HLA-B*27:72','HLA-B*27:73','HLA-B*27:74','HLA-B*27:75','HLA-B*27:76','HLA-B*27:77','HLA-B*27:78','HLA-B*27:79','HLA-B*27:80','HLA-B*27:81','HLA-B*27:82','HLA-B*27:83','HLA-B*27:84','HLA-B*27:85','HLA-B*27:86','HLA-B*27:87','HLA-B*27:88','HLA-B*27:89','HLA-B*27:90','HLA-B*27:91','HLA-B*27:92','HLA-B*27:93','HLA-B*27:95','HLA-B*27:96','HLA-B*27:97','HLA-B*27:98','HLA-B*27:99','HLA-B*35:01','HLA-B*35:02','HLA-B*35:03','HLA-B*35:04','HLA-B*35:05','HLA-B*35:06','HLA-B*35:07','HLA-B*35:08','HLA-B*35:09','HLA-B*35:10','HLA-B*35:100','HLA-B*35:101','HLA-B*35:102','HLA-B*35:103','HLA-B*35:104','HLA-B*35:105','HLA-B*35:106','HLA-B*35:107','HLA-B*35:108','HLA-B*35:109','HLA-B*35:11','HLA-B*35:110','HLA-B*35:111','HLA-B*35:112','HLA-B*35:113','HLA-B*35:114','HLA-B*35:115','HLA-B*35:116','HLA-B*35:117','HLA-B*35:118','HLA-B*35:119','HLA-B*35:12','HLA-B*35:120','HLA-B*35:121','HLA-B*35:122','HLA-B*35:123','HLA-B*35:124','HLA-B*35:125','HLA-B*35:126','HLA-B*35:127','HLA-B*35:128','HLA-B*35:13','HLA-B*35:131','HLA-B*35:132','HLA-B*35:133','HLA-B*35:135','HLA-B*35:136','HLA-B*35:137','HLA-B*35:138','HLA-B*35:139','HLA-B*35:14','HLA-B*35:140','HLA-B*35:141','HLA-B*35:142','HLA-B*35:143','HLA-B*35:144','HLA-B*35:146','HLA-B*35:147','HLA-B*35:148','HLA-B*35:149','HLA-B*35:15','HLA-B*35:150','HLA-B*35:151','HLA-B*35:152','HLA-B*35:153','HLA-B*35:154','HLA-B*35:155','HLA-B*35:156','HLA-B*35:157','HLA-B*35:158','HLA-B*35:159','HLA-B*35:16','HLA-B*35:160','HLA-B*35:161','HLA-B*35:162','HLA-B*35:163','HLA-B*35:164','HLA-B*35:166','HLA-B*35:167','HLA-B*35:168','HLA-B*35:169','HLA-B*35:17','HLA-B*35:170','HLA-B*35:171','HLA-B*35:172','HLA-B*35:174','HLA-B*35:175','HLA-B*35:176','HLA-B*35:177','HLA-B*35:178','HLA-B*35:179','HLA-B*35:18','HLA-B*35:180','HLA-B*35:181','HLA-B*35:182','HLA-B*35:183','HLA-B*35:184','HLA-B*35:185','HLA-B*35:186','HLA-B*35:187','HLA-B*35:188','HLA-B*35:189','HLA-B*35:19','HLA-B*35:190','HLA-B*35:191','HLA-B*35:192','HLA-B*35:193','HLA-B*35:194','HLA-B*35:195','HLA-B*35:196','HLA-B*35:197','HLA-B*35:198','HLA-B*35:199','HLA-B*35:20','HLA-B*35:200','HLA-B*35:201','HLA-B*35:202','HLA-B*35:203','HLA-B*35:204','HLA-B*35:205','HLA-B*35:206','HLA-B*35:207','HLA-B*35:208','HLA-B*35:209','HLA-B*35:21','HLA-B*35:210','HLA-B*35:211','HLA-B*35:212','HLA-B*35:213','HLA-B*35:214','HLA-B*35:215','HLA-B*35:217','HLA-B*35:218','HLA-B*35:219','HLA-B*35:22','HLA-B*35:220','HLA-B*35:221','HLA-B*35:222','HLA-B*35:223','HLA-B*35:224','HLA-B*35:225','HLA-B*35:226','HLA-B*35:227','HLA-B*35:228','HLA-B*35:229','HLA-B*35:23','HLA-B*35:230','HLA-B*35:231','HLA-B*35:232','HLA-B*35:233','HLA-B*35:234','HLA-B*35:235','HLA-B*35:236','HLA-B*35:237','HLA-B*35:238','HLA-B*35:239','HLA-B*35:24','HLA-B*35:240','HLA-B*35:241','HLA-B*35:242','HLA-B*35:243','HLA-B*35:244','HLA-B*35:245','HLA-B*35:246','HLA-B*35:247','HLA-B*35:248','HLA-B*35:249','HLA-B*35:25','HLA-B*35:250','HLA-B*35:251','HLA-B*35:252','HLA-B*35:253','HLA-B*35:254','HLA-B*35:255','HLA-B*35:256','HLA-B*35:257','HLA-B*35:258','HLA-B*35:259','HLA-B*35:26','HLA-B*35:260','HLA-B*35:261','HLA-B*35:262','HLA-B*35:263','HLA-B*35:264','HLA-B*35:265','HLA-B*35:266','HLA-B*35:267','HLA-B*35:268','HLA-B*35:269','HLA-B*35:27','HLA-B*35:270','HLA-B*35:271','HLA-B*35:272','HLA-B*35:273','HLA-B*35:274','HLA-B*35:275','HLA-B*35:276','HLA-B*35:277','HLA-B*35:278','HLA-B*35:279','HLA-B*35:28','HLA-B*35:280','HLA-B*35:281','HLA-B*35:282','HLA-B*35:283','HLA-B*35:284','HLA-B*35:285','HLA-B*35:286','HLA-B*35:287','HLA-B*35:288','HLA-B*35:289','HLA-B*35:29','HLA-B*35:290','HLA-B*35:291','HLA-B*35:292','HLA-B*35:293','HLA-B*35:294','HLA-B*35:295','HLA-B*35:296','HLA-B*35:297','HLA-B*35:298','HLA-B*35:299','HLA-B*35:30','HLA-B*35:300','HLA-B*35:301','HLA-B*35:302','HLA-B*35:303','HLA-B*35:304','HLA-B*35:305','HLA-B*35:306','HLA-B*35:307','HLA-B*35:308','HLA-B*35:309','HLA-B*35:31','HLA-B*35:310','HLA-B*35:311','HLA-B*35:312','HLA-B*35:313','HLA-B*35:314','HLA-B*35:315','HLA-B*35:316','HLA-B*35:317','HLA-B*35:318','HLA-B*35:319','HLA-B*35:32','HLA-B*35:320','HLA-B*35:321','HLA-B*35:322','HLA-B*35:323','HLA-B*35:324','HLA-B*35:325','HLA-B*35:326','HLA-B*35:327','HLA-B*35:328','HLA-B*35:329','HLA-B*35:33','HLA-B*35:330','HLA-B*35:331','HLA-B*35:332','HLA-B*35:334','HLA-B*35:335','HLA-B*35:336','HLA-B*35:337','HLA-B*35:338','HLA-B*35:339','HLA-B*35:34','HLA-B*35:340','HLA-B*35:341','HLA-B*35:342','HLA-B*35:343','HLA-B*35:344','HLA-B*35:345','HLA-B*35:346','HLA-B*35:347','HLA-B*35:348','HLA-B*35:349','HLA-B*35:35','HLA-B*35:350','HLA-B*35:351','HLA-B*35:352','HLA-B*35:353','HLA-B*35:354','HLA-B*35:355','HLA-B*35:356','HLA-B*35:357','HLA-B*35:358','HLA-B*35:359','HLA-B*35:36','HLA-B*35:360','HLA-B*35:361','HLA-B*35:362','HLA-B*35:363','HLA-B*35:364','HLA-B*35:365','HLA-B*35:366','HLA-B*35:367','HLA-B*35:368','HLA-B*35:369','HLA-B*35:37','HLA-B*35:370','HLA-B*35:371','HLA-B*35:372','HLA-B*35:373','HLA-B*35:374','HLA-B*35:375','HLA-B*35:376','HLA-B*35:377','HLA-B*35:378','HLA-B*35:379','HLA-B*35:38','HLA-B*35:380','HLA-B*35:382','HLA-B*35:383','HLA-B*35:384','HLA-B*35:385','HLA-B*35:386','HLA-B*35:387','HLA-B*35:388','HLA-B*35:389','HLA-B*35:39','HLA-B*35:391','HLA-B*35:392','HLA-B*35:393','HLA-B*35:394','HLA-B*35:395','HLA-B*35:396','HLA-B*35:397','HLA-B*35:398','HLA-B*35:399','HLA-B*35:400','HLA-B*35:401','HLA-B*35:402','HLA-B*35:403','HLA-B*35:404','HLA-B*35:405','HLA-B*35:406','HLA-B*35:407','HLA-B*35:408','HLA-B*35:409','HLA-B*35:41','HLA-B*35:410','HLA-B*35:411','HLA-B*35:412','HLA-B*35:413','HLA-B*35:42','HLA-B*35:43','HLA-B*35:44','HLA-B*35:45','HLA-B*35:46','HLA-B*35:47','HLA-B*35:48','HLA-B*35:49','HLA-B*35:50','HLA-B*35:51','HLA-B*35:52','HLA-B*35:54','HLA-B*35:55','HLA-B*35:56','HLA-B*35:57','HLA-B*35:58','HLA-B*35:59','HLA-B*35:60','HLA-B*35:61','HLA-B*35:62','HLA-B*35:63','HLA-B*35:64','HLA-B*35:66','HLA-B*35:67','HLA-B*35:68','HLA-B*35:69','HLA-B*35:70','HLA-B*35:71','HLA-B*35:72','HLA-B*35:74','HLA-B*35:75','HLA-B*35:76','HLA-B*35:77','HLA-B*35:78','HLA-B*35:79','HLA-B*35:80','HLA-B*35:81','HLA-B*35:82','HLA-B*35:83','HLA-B*35:84','HLA-B*35:85','HLA-B*35:86','HLA-B*35:87','HLA-B*35:88','HLA-B*35:89','HLA-B*35:90','HLA-B*35:91','HLA-B*35:92','HLA-B*35:93','HLA-B*35:94','HLA-B*35:95','HLA-B*35:96','HLA-B*35:97','HLA-B*35:98','HLA-B*35:99','HLA-B*37:01','HLA-B*37:02','HLA-B*37:04','HLA-B*37:05','HLA-B*37:06','HLA-B*37:07','HLA-B*37:08','HLA-B*37:09','HLA-B*37:10','HLA-B*37:11','HLA-B*37:12','HLA-B*37:13','HLA-B*37:14','HLA-B*37:15','HLA-B*37:17','HLA-B*37:18','HLA-B*37:19','HLA-B*37:20','HLA-B*37:21','HLA-B*37:22','HLA-B*37:23','HLA-B*37:24','HLA-B*37:25','HLA-B*37:26','HLA-B*37:27','HLA-B*37:28','HLA-B*37:29','HLA-B*37:31','HLA-B*37:32','HLA-B*37:34','HLA-B*37:35','HLA-B*37:36','HLA-B*37:37','HLA-B*37:38','HLA-B*37:39','HLA-B*37:40','HLA-B*37:41','HLA-B*37:43','HLA-B*37:44','HLA-B*37:45','HLA-B*37:46','HLA-B*37:47','HLA-B*37:48','HLA-B*37:49','HLA-B*37:50','HLA-B*37:51','HLA-B*37:52','HLA-B*37:53','HLA-B*37:54','HLA-B*37:55','HLA-B*37:56','HLA-B*37:57','HLA-B*37:58','HLA-B*37:59','HLA-B*37:60','HLA-B*37:61','HLA-B*37:62','HLA-B*37:63','HLA-B*37:64','HLA-B*37:65','HLA-B*37:66','HLA-B*37:67','HLA-B*37:68','HLA-B*37:69','HLA-B*37:70','HLA-B*37:71','HLA-B*37:72','HLA-B*37:73','HLA-B*37:74','HLA-B*37:75','HLA-B*37:76','HLA-B*37:77','HLA-B*37:78','HLA-B*37:80','HLA-B*38:01','HLA-B*38:02','HLA-B*38:03','HLA-B*38:04','HLA-B*38:05','HLA-B*38:06','HLA-B*38:07','HLA-B*38:08','HLA-B*38:09','HLA-B*38:10','HLA-B*38:11','HLA-B*38:12','HLA-B*38:13','HLA-B*38:14','HLA-B*38:15','HLA-B*38:16','HLA-B*38:17','HLA-B*38:18','HLA-B*38:19','HLA-B*38:20','HLA-B*38:21','HLA-B*38:22','HLA-B*38:23','HLA-B*38:24','HLA-B*38:25','HLA-B*38:26','HLA-B*38:27','HLA-B*38:28','HLA-B*38:29','HLA-B*38:30','HLA-B*38:31','HLA-B*38:32','HLA-B*38:33','HLA-B*38:35','HLA-B*38:36','HLA-B*38:37','HLA-B*38:38','HLA-B*38:39','HLA-B*38:40','HLA-B*38:41','HLA-B*38:42','HLA-B*38:43','HLA-B*38:44','HLA-B*38:45','HLA-B*38:46','HLA-B*38:47','HLA-B*38:48','HLA-B*38:49','HLA-B*38:50','HLA-B*38:51','HLA-B*38:52','HLA-B*38:53','HLA-B*38:54','HLA-B*38:56','HLA-B*38:57','HLA-B*38:58','HLA-B*38:59','HLA-B*38:60','HLA-B*38:61','HLA-B*38:62','HLA-B*38:63','HLA-B*38:64','HLA-B*38:65','HLA-B*38:66','HLA-B*38:67','HLA-B*38:69','HLA-B*38:70','HLA-B*38:71','HLA-B*38:72','HLA-B*38:73','HLA-B*38:74','HLA-B*38:75','HLA-B*38:76','HLA-B*38:77','HLA-B*38:78','HLA-B*38:79','HLA-B*38:81','HLA-B*38:82','HLA-B*39:01','HLA-B*39:02','HLA-B*39:03','HLA-B*39:04','HLA-B*39:05','HLA-B*39:06','HLA-B*39:07','HLA-B*39:08','HLA-B*39:09','HLA-B*39:10','HLA-B*39:100','HLA-B*39:101','HLA-B*39:102','HLA-B*39:103','HLA-B*39:104','HLA-B*39:105','HLA-B*39:106','HLA-B*39:107','HLA-B*39:108','HLA-B*39:109','HLA-B*39:11','HLA-B*39:110','HLA-B*39:111','HLA-B*39:112','HLA-B*39:113','HLA-B*39:114','HLA-B*39:115','HLA-B*39:117','HLA-B*39:118','HLA-B*39:119','HLA-B*39:12','HLA-B*39:120','HLA-B*39:121','HLA-B*39:122','HLA-B*39:123','HLA-B*39:124','HLA-B*39:125','HLA-B*39:126','HLA-B*39:127','HLA-B*39:128','HLA-B*39:129','HLA-B*39:13','HLA-B*39:130','HLA-B*39:131','HLA-B*39:132','HLA-B*39:134','HLA-B*39:135','HLA-B*39:136','HLA-B*39:137','HLA-B*39:138','HLA-B*39:14','HLA-B*39:140','HLA-B*39:141','HLA-B*39:143','HLA-B*39:15','HLA-B*39:16','HLA-B*39:17','HLA-B*39:18','HLA-B*39:19','HLA-B*39:20','HLA-B*39:22','HLA-B*39:23','HLA-B*39:24','HLA-B*39:26','HLA-B*39:27','HLA-B*39:28','HLA-B*39:29','HLA-B*39:30','HLA-B*39:31','HLA-B*39:32','HLA-B*39:33','HLA-B*39:34','HLA-B*39:35','HLA-B*39:36','HLA-B*39:37','HLA-B*39:39','HLA-B*39:41','HLA-B*39:42','HLA-B*39:43','HLA-B*39:44','HLA-B*39:45','HLA-B*39:46','HLA-B*39:47','HLA-B*39:48','HLA-B*39:49','HLA-B*39:50','HLA-B*39:51','HLA-B*39:52','HLA-B*39:53','HLA-B*39:54','HLA-B*39:55','HLA-B*39:56','HLA-B*39:57','HLA-B*39:58','HLA-B*39:59','HLA-B*39:60','HLA-B*39:61','HLA-B*39:62','HLA-B*39:63','HLA-B*39:64','HLA-B*39:65','HLA-B*39:66','HLA-B*39:67','HLA-B*39:68','HLA-B*39:69','HLA-B*39:70','HLA-B*39:71','HLA-B*39:72','HLA-B*39:73','HLA-B*39:74','HLA-B*39:75','HLA-B*39:76','HLA-B*39:77','HLA-B*39:78','HLA-B*39:79','HLA-B*39:80','HLA-B*39:81','HLA-B*39:82','HLA-B*39:83','HLA-B*39:84','HLA-B*39:85','HLA-B*39:86','HLA-B*39:88','HLA-B*39:89','HLA-B*39:90','HLA-B*39:91','HLA-B*39:92','HLA-B*39:93','HLA-B*39:94','HLA-B*39:96','HLA-B*39:98','HLA-B*39:99','HLA-B*40:01','HLA-B*40:02','HLA-B*40:03','HLA-B*40:04','HLA-B*40:05','HLA-B*40:06','HLA-B*40:07','HLA-B*40:08','HLA-B*40:09','HLA-B*40:10','HLA-B*40:100','HLA-B*40:101','HLA-B*40:102','HLA-B*40:103','HLA-B*40:104','HLA-B*40:105','HLA-B*40:106','HLA-B*40:107','HLA-B*40:108','HLA-B*40:109','HLA-B*40:11','HLA-B*40:110','HLA-B*40:111','HLA-B*40:112','HLA-B*40:113','HLA-B*40:114','HLA-B*40:115','HLA-B*40:116','HLA-B*40:117','HLA-B*40:119','HLA-B*40:12','HLA-B*40:120','HLA-B*40:121','HLA-B*40:122','HLA-B*40:123','HLA-B*40:124','HLA-B*40:125','HLA-B*40:126','HLA-B*40:127','HLA-B*40:128','HLA-B*40:129','HLA-B*40:13','HLA-B*40:130','HLA-B*40:131','HLA-B*40:132','HLA-B*40:134','HLA-B*40:135','HLA-B*40:136','HLA-B*40:137','HLA-B*40:138','HLA-B*40:139','HLA-B*40:14','HLA-B*40:140','HLA-B*40:141','HLA-B*40:143','HLA-B*40:145','HLA-B*40:146','HLA-B*40:147','HLA-B*40:148','HLA-B*40:149','HLA-B*40:15','HLA-B*40:150','HLA-B*40:151','HLA-B*40:152','HLA-B*40:153','HLA-B*40:154','HLA-B*40:155','HLA-B*40:156','HLA-B*40:157','HLA-B*40:158','HLA-B*40:159','HLA-B*40:16','HLA-B*40:160','HLA-B*40:161','HLA-B*40:162','HLA-B*40:163','HLA-B*40:164','HLA-B*40:165','HLA-B*40:166','HLA-B*40:167','HLA-B*40:168','HLA-B*40:169','HLA-B*40:170','HLA-B*40:171','HLA-B*40:172','HLA-B*40:173','HLA-B*40:174','HLA-B*40:175','HLA-B*40:176','HLA-B*40:177','HLA-B*40:178','HLA-B*40:179','HLA-B*40:18','HLA-B*40:180','HLA-B*40:181','HLA-B*40:182','HLA-B*40:183','HLA-B*40:184','HLA-B*40:185','HLA-B*40:186','HLA-B*40:187','HLA-B*40:188','HLA-B*40:189','HLA-B*40:19','HLA-B*40:190','HLA-B*40:191','HLA-B*40:192','HLA-B*40:193','HLA-B*40:194','HLA-B*40:195','HLA-B*40:196','HLA-B*40:197','HLA-B*40:198','HLA-B*40:199','HLA-B*40:20','HLA-B*40:200','HLA-B*40:201','HLA-B*40:202','HLA-B*40:203','HLA-B*40:204','HLA-B*40:205','HLA-B*40:206','HLA-B*40:207','HLA-B*40:208','HLA-B*40:209','HLA-B*40:21','HLA-B*40:210','HLA-B*40:211','HLA-B*40:212','HLA-B*40:213','HLA-B*40:214','HLA-B*40:215','HLA-B*40:217','HLA-B*40:218','HLA-B*40:219','HLA-B*40:220','HLA-B*40:221','HLA-B*40:222','HLA-B*40:223','HLA-B*40:224','HLA-B*40:225','HLA-B*40:226','HLA-B*40:227','HLA-B*40:228','HLA-B*40:229','HLA-B*40:23','HLA-B*40:230','HLA-B*40:231','HLA-B*40:232','HLA-B*40:233','HLA-B*40:234','HLA-B*40:235','HLA-B*40:236','HLA-B*40:237','HLA-B*40:238','HLA-B*40:239','HLA-B*40:24','HLA-B*40:240','HLA-B*40:241','HLA-B*40:242','HLA-B*40:243','HLA-B*40:244','HLA-B*40:245','HLA-B*40:246','HLA-B*40:247','HLA-B*40:248','HLA-B*40:249','HLA-B*40:25','HLA-B*40:250','HLA-B*40:251','HLA-B*40:252','HLA-B*40:253','HLA-B*40:254','HLA-B*40:255','HLA-B*40:257','HLA-B*40:258','HLA-B*40:259','HLA-B*40:26','HLA-B*40:260','HLA-B*40:261','HLA-B*40:262','HLA-B*40:264','HLA-B*40:266','HLA-B*40:267','HLA-B*40:268','HLA-B*40:269','HLA-B*40:27','HLA-B*40:270','HLA-B*40:271','HLA-B*40:272','HLA-B*40:273','HLA-B*40:274','HLA-B*40:275','HLA-B*40:276','HLA-B*40:277','HLA-B*40:278','HLA-B*40:279','HLA-B*40:28','HLA-B*40:280','HLA-B*40:281','HLA-B*40:282','HLA-B*40:283','HLA-B*40:284','HLA-B*40:285','HLA-B*40:287','HLA-B*40:288','HLA-B*40:289','HLA-B*40:29','HLA-B*40:290','HLA-B*40:292','HLA-B*40:293','HLA-B*40:294','HLA-B*40:295','HLA-B*40:296','HLA-B*40:297','HLA-B*40:298','HLA-B*40:299','HLA-B*40:30','HLA-B*40:300','HLA-B*40:301','HLA-B*40:302','HLA-B*40:303','HLA-B*40:304','HLA-B*40:305','HLA-B*40:306','HLA-B*40:307','HLA-B*40:308','HLA-B*40:309','HLA-B*40:31','HLA-B*40:310','HLA-B*40:311','HLA-B*40:312','HLA-B*40:313','HLA-B*40:314','HLA-B*40:315','HLA-B*40:316','HLA-B*40:317','HLA-B*40:318','HLA-B*40:319','HLA-B*40:32','HLA-B*40:320','HLA-B*40:321','HLA-B*40:322','HLA-B*40:323','HLA-B*40:324','HLA-B*40:325','HLA-B*40:326','HLA-B*40:327','HLA-B*40:328','HLA-B*40:329','HLA-B*40:33','HLA-B*40:330','HLA-B*40:331','HLA-B*40:332','HLA-B*40:333','HLA-B*40:334','HLA-B*40:335','HLA-B*40:336','HLA-B*40:339','HLA-B*40:34','HLA-B*40:340','HLA-B*40:341','HLA-B*40:342','HLA-B*40:343','HLA-B*40:344','HLA-B*40:346','HLA-B*40:347','HLA-B*40:348','HLA-B*40:349','HLA-B*40:35','HLA-B*40:350','HLA-B*40:351','HLA-B*40:352','HLA-B*40:353','HLA-B*40:354','HLA-B*40:355','HLA-B*40:356','HLA-B*40:357','HLA-B*40:358','HLA-B*40:359','HLA-B*40:36','HLA-B*40:360','HLA-B*40:362','HLA-B*40:363','HLA-B*40:364','HLA-B*40:365','HLA-B*40:366','HLA-B*40:367','HLA-B*40:368','HLA-B*40:369','HLA-B*40:37','HLA-B*40:370','HLA-B*40:371','HLA-B*40:373','HLA-B*40:374','HLA-B*40:375','HLA-B*40:376','HLA-B*40:377','HLA-B*40:378','HLA-B*40:379','HLA-B*40:38','HLA-B*40:380','HLA-B*40:381','HLA-B*40:382','HLA-B*40:383','HLA-B*40:384','HLA-B*40:385','HLA-B*40:386','HLA-B*40:387','HLA-B*40:388','HLA-B*40:389','HLA-B*40:39','HLA-B*40:390','HLA-B*40:391','HLA-B*40:392','HLA-B*40:393','HLA-B*40:394','HLA-B*40:395','HLA-B*40:396','HLA-B*40:397','HLA-B*40:398','HLA-B*40:40','HLA-B*40:400','HLA-B*40:401','HLA-B*40:402','HLA-B*40:403','HLA-B*40:404','HLA-B*40:406','HLA-B*40:407','HLA-B*40:408','HLA-B*40:409','HLA-B*40:410','HLA-B*40:411','HLA-B*40:412','HLA-B*40:413','HLA-B*40:414','HLA-B*40:42','HLA-B*40:43','HLA-B*40:44','HLA-B*40:45','HLA-B*40:46','HLA-B*40:47','HLA-B*40:48','HLA-B*40:49','HLA-B*40:50','HLA-B*40:51','HLA-B*40:52','HLA-B*40:53','HLA-B*40:54','HLA-B*40:55','HLA-B*40:56','HLA-B*40:57','HLA-B*40:58','HLA-B*40:59','HLA-B*40:60','HLA-B*40:61','HLA-B*40:62','HLA-B*40:63','HLA-B*40:64','HLA-B*40:65','HLA-B*40:66','HLA-B*40:67','HLA-B*40:68','HLA-B*40:69','HLA-B*40:70','HLA-B*40:71','HLA-B*40:72','HLA-B*40:73','HLA-B*40:74','HLA-B*40:75','HLA-B*40:76','HLA-B*40:77','HLA-B*40:78','HLA-B*40:79','HLA-B*40:80','HLA-B*40:81','HLA-B*40:82','HLA-B*40:83','HLA-B*40:84','HLA-B*40:85','HLA-B*40:86','HLA-B*40:87','HLA-B*40:88','HLA-B*40:89','HLA-B*40:90','HLA-B*40:91','HLA-B*40:92','HLA-B*40:93','HLA-B*40:94','HLA-B*40:95','HLA-B*40:96','HLA-B*40:97','HLA-B*40:98','HLA-B*40:99','HLA-B*41:01','HLA-B*41:02','HLA-B*41:03','HLA-B*41:04','HLA-B*41:05','HLA-B*41:06','HLA-B*41:07','HLA-B*41:08','HLA-B*41:09','HLA-B*41:10','HLA-B*41:11','HLA-B*41:12','HLA-B*41:13','HLA-B*41:14','HLA-B*41:15','HLA-B*41:16','HLA-B*41:17','HLA-B*41:18','HLA-B*41:19','HLA-B*41:20','HLA-B*41:21','HLA-B*41:22','HLA-B*41:23','HLA-B*41:24','HLA-B*41:25','HLA-B*41:26','HLA-B*41:27','HLA-B*41:28','HLA-B*41:29','HLA-B*41:30','HLA-B*41:31','HLA-B*41:32','HLA-B*41:33','HLA-B*41:34','HLA-B*41:35','HLA-B*41:36','HLA-B*41:37','HLA-B*41:38','HLA-B*41:39','HLA-B*41:40','HLA-B*41:41','HLA-B*41:42','HLA-B*41:43','HLA-B*41:44','HLA-B*41:46','HLA-B*41:47','HLA-B*41:48','HLA-B*41:49','HLA-B*41:50','HLA-B*41:51','HLA-B*41:52','HLA-B*41:53','HLA-B*41:54','HLA-B*41:55','HLA-B*41:56','HLA-B*42:01','HLA-B*42:02','HLA-B*42:04','HLA-B*42:05','HLA-B*42:06','HLA-B*42:07','HLA-B*42:08','HLA-B*42:09','HLA-B*42:10','HLA-B*42:11','HLA-B*42:12','HLA-B*42:13','HLA-B*42:14','HLA-B*42:15','HLA-B*42:16','HLA-B*42:17','HLA-B*42:18','HLA-B*42:19','HLA-B*42:20','HLA-B*42:21','HLA-B*42:22','HLA-B*42:23','HLA-B*42:24','HLA-B*42:25','HLA-B*44:02','HLA-B*44:03','HLA-B*44:04','HLA-B*44:05','HLA-B*44:06','HLA-B*44:07','HLA-B*44:08','HLA-B*44:09','HLA-B*44:10','HLA-B*44:100','HLA-B*44:101','HLA-B*44:102','HLA-B*44:103','HLA-B*44:104','HLA-B*44:105','HLA-B*44:106','HLA-B*44:107','HLA-B*44:109','HLA-B*44:11','HLA-B*44:110','HLA-B*44:111','HLA-B*44:112','HLA-B*44:113','HLA-B*44:114','HLA-B*44:115','HLA-B*44:116','HLA-B*44:117','HLA-B*44:118','HLA-B*44:119','HLA-B*44:12','HLA-B*44:120','HLA-B*44:121','HLA-B*44:122','HLA-B*44:123','HLA-B*44:124','HLA-B*44:125','HLA-B*44:126','HLA-B*44:127','HLA-B*44:128','HLA-B*44:129','HLA-B*44:13','HLA-B*44:130','HLA-B*44:131','HLA-B*44:132','HLA-B*44:133','HLA-B*44:134','HLA-B*44:135','HLA-B*44:136','HLA-B*44:137','HLA-B*44:139','HLA-B*44:14','HLA-B*44:140','HLA-B*44:141','HLA-B*44:142','HLA-B*44:143','HLA-B*44:144','HLA-B*44:145','HLA-B*44:146','HLA-B*44:147','HLA-B*44:148','HLA-B*44:15','HLA-B*44:150','HLA-B*44:151','HLA-B*44:152','HLA-B*44:153','HLA-B*44:154','HLA-B*44:155','HLA-B*44:156','HLA-B*44:157','HLA-B*44:158','HLA-B*44:159','HLA-B*44:16','HLA-B*44:161','HLA-B*44:162','HLA-B*44:163','HLA-B*44:164','HLA-B*44:165','HLA-B*44:166','HLA-B*44:167','HLA-B*44:168','HLA-B*44:169','HLA-B*44:17','HLA-B*44:170','HLA-B*44:172','HLA-B*44:173','HLA-B*44:174','HLA-B*44:175','HLA-B*44:176','HLA-B*44:177','HLA-B*44:178','HLA-B*44:179','HLA-B*44:18','HLA-B*44:180','HLA-B*44:181','HLA-B*44:182','HLA-B*44:183','HLA-B*44:184','HLA-B*44:185','HLA-B*44:186','HLA-B*44:187','HLA-B*44:188','HLA-B*44:189','HLA-B*44:190','HLA-B*44:191','HLA-B*44:192','HLA-B*44:193','HLA-B*44:194','HLA-B*44:196','HLA-B*44:197','HLA-B*44:199','HLA-B*44:20','HLA-B*44:200','HLA-B*44:201','HLA-B*44:202','HLA-B*44:203','HLA-B*44:204','HLA-B*44:205','HLA-B*44:206','HLA-B*44:207','HLA-B*44:208','HLA-B*44:209','HLA-B*44:21','HLA-B*44:210','HLA-B*44:211','HLA-B*44:212','HLA-B*44:213','HLA-B*44:214','HLA-B*44:215','HLA-B*44:216','HLA-B*44:218','HLA-B*44:219','HLA-B*44:22','HLA-B*44:220','HLA-B*44:221','HLA-B*44:222','HLA-B*44:223','HLA-B*44:224','HLA-B*44:225','HLA-B*44:226','HLA-B*44:227','HLA-B*44:228','HLA-B*44:229','HLA-B*44:230','HLA-B*44:231','HLA-B*44:232','HLA-B*44:233','HLA-B*44:234','HLA-B*44:235','HLA-B*44:236','HLA-B*44:238','HLA-B*44:239','HLA-B*44:24','HLA-B*44:240','HLA-B*44:241','HLA-B*44:242','HLA-B*44:243','HLA-B*44:244','HLA-B*44:245','HLA-B*44:247','HLA-B*44:248','HLA-B*44:249','HLA-B*44:25','HLA-B*44:250','HLA-B*44:251','HLA-B*44:252','HLA-B*44:253','HLA-B*44:254','HLA-B*44:255','HLA-B*44:256','HLA-B*44:257','HLA-B*44:258','HLA-B*44:259','HLA-B*44:26','HLA-B*44:260','HLA-B*44:261','HLA-B*44:262','HLA-B*44:263','HLA-B*44:264','HLA-B*44:265','HLA-B*44:266','HLA-B*44:268','HLA-B*44:269','HLA-B*44:27','HLA-B*44:270','HLA-B*44:271','HLA-B*44:272','HLA-B*44:273','HLA-B*44:274','HLA-B*44:275','HLA-B*44:276','HLA-B*44:277','HLA-B*44:278','HLA-B*44:279','HLA-B*44:28','HLA-B*44:280','HLA-B*44:281','HLA-B*44:282','HLA-B*44:283','HLA-B*44:284','HLA-B*44:285','HLA-B*44:286','HLA-B*44:287','HLA-B*44:288','HLA-B*44:289','HLA-B*44:29','HLA-B*44:290','HLA-B*44:291','HLA-B*44:292','HLA-B*44:293','HLA-B*44:294','HLA-B*44:295','HLA-B*44:296','HLA-B*44:297','HLA-B*44:298','HLA-B*44:299','HLA-B*44:30','HLA-B*44:300','HLA-B*44:301','HLA-B*44:302','HLA-B*44:304','HLA-B*44:305','HLA-B*44:307','HLA-B*44:308','HLA-B*44:31','HLA-B*44:311','HLA-B*44:312','HLA-B*44:313','HLA-B*44:315','HLA-B*44:316','HLA-B*44:317','HLA-B*44:318','HLA-B*44:319','HLA-B*44:32','HLA-B*44:320','HLA-B*44:321','HLA-B*44:322','HLA-B*44:323','HLA-B*44:324','HLA-B*44:325','HLA-B*44:326','HLA-B*44:327','HLA-B*44:329','HLA-B*44:33','HLA-B*44:330','HLA-B*44:331','HLA-B*44:332','HLA-B*44:34','HLA-B*44:35','HLA-B*44:36','HLA-B*44:37','HLA-B*44:38','HLA-B*44:39','HLA-B*44:40','HLA-B*44:41','HLA-B*44:42','HLA-B*44:43','HLA-B*44:44','HLA-B*44:45','HLA-B*44:46','HLA-B*44:47','HLA-B*44:48','HLA-B*44:49','HLA-B*44:50','HLA-B*44:51','HLA-B*44:53','HLA-B*44:54','HLA-B*44:55','HLA-B*44:57','HLA-B*44:59','HLA-B*44:60','HLA-B*44:62','HLA-B*44:63','HLA-B*44:64','HLA-B*44:65','HLA-B*44:66','HLA-B*44:67','HLA-B*44:68','HLA-B*44:69','HLA-B*44:70','HLA-B*44:71','HLA-B*44:72','HLA-B*44:73','HLA-B*44:74','HLA-B*44:75','HLA-B*44:76','HLA-B*44:77','HLA-B*44:78','HLA-B*44:79','HLA-B*44:80','HLA-B*44:81','HLA-B*44:82','HLA-B*44:83','HLA-B*44:84','HLA-B*44:85','HLA-B*44:86','HLA-B*44:87','HLA-B*44:88','HLA-B*44:89','HLA-B*44:90','HLA-B*44:91','HLA-B*44:92','HLA-B*44:93','HLA-B*44:94','HLA-B*44:95','HLA-B*44:96','HLA-B*44:97','HLA-B*44:98','HLA-B*44:99','HLA-B*45:01','HLA-B*45:02','HLA-B*45:03','HLA-B*45:04','HLA-B*45:05','HLA-B*45:06','HLA-B*45:07','HLA-B*45:08','HLA-B*45:09','HLA-B*45:10','HLA-B*45:11','HLA-B*45:12','HLA-B*45:13','HLA-B*45:14','HLA-B*45:15','HLA-B*45:16','HLA-B*45:17','HLA-B*45:18','HLA-B*45:19','HLA-B*45:20','HLA-B*45:21','HLA-B*45:22','HLA-B*45:23','HLA-B*45:24','HLA-B*46:01','HLA-B*46:02','HLA-B*46:03','HLA-B*46:04','HLA-B*46:05','HLA-B*46:06','HLA-B*46:08','HLA-B*46:09','HLA-B*46:10','HLA-B*46:11','HLA-B*46:12','HLA-B*46:13','HLA-B*46:14','HLA-B*46:16','HLA-B*46:17','HLA-B*46:18','HLA-B*46:19','HLA-B*46:20','HLA-B*46:21','HLA-B*46:22','HLA-B*46:23','HLA-B*46:24','HLA-B*46:25','HLA-B*46:26','HLA-B*46:27','HLA-B*46:28','HLA-B*46:29','HLA-B*46:30','HLA-B*46:31','HLA-B*46:32','HLA-B*46:33','HLA-B*46:34','HLA-B*46:35','HLA-B*46:36','HLA-B*46:37','HLA-B*46:38','HLA-B*46:39','HLA-B*46:40','HLA-B*46:42','HLA-B*46:43','HLA-B*46:44','HLA-B*46:45','HLA-B*46:46','HLA-B*46:47','HLA-B*46:48','HLA-B*46:49','HLA-B*46:50','HLA-B*46:52','HLA-B*46:53','HLA-B*46:54','HLA-B*46:56','HLA-B*46:57','HLA-B*46:58','HLA-B*46:59','HLA-B*46:60','HLA-B*46:61','HLA-B*46:62','HLA-B*46:63','HLA-B*46:64','HLA-B*46:65','HLA-B*46:66','HLA-B*46:67','HLA-B*46:68','HLA-B*46:69','HLA-B*46:70','HLA-B*46:71','HLA-B*46:72','HLA-B*46:73','HLA-B*46:74','HLA-B*46:75','HLA-B*47:01','HLA-B*47:02','HLA-B*47:03','HLA-B*47:04','HLA-B*47:05','HLA-B*47:06','HLA-B*47:07','HLA-B*47:08','HLA-B*47:09','HLA-B*47:10','HLA-B*48:01','HLA-B*48:02','HLA-B*48:03','HLA-B*48:04','HLA-B*48:05','HLA-B*48:06','HLA-B*48:07','HLA-B*48:08','HLA-B*48:09','HLA-B*48:10','HLA-B*48:11','HLA-B*48:12','HLA-B*48:13','HLA-B*48:14','HLA-B*48:15','HLA-B*48:16','HLA-B*48:17','HLA-B*48:18','HLA-B*48:19','HLA-B*48:20','HLA-B*48:21','HLA-B*48:22','HLA-B*48:23','HLA-B*48:24','HLA-B*48:25','HLA-B*48:26','HLA-B*48:27','HLA-B*48:28','HLA-B*48:29','HLA-B*48:30','HLA-B*48:31','HLA-B*48:32','HLA-B*48:33','HLA-B*48:34','HLA-B*48:35','HLA-B*48:36','HLA-B*48:37','HLA-B*48:38','HLA-B*48:39','HLA-B*48:40','HLA-B*48:41','HLA-B*48:42','HLA-B*48:43','HLA-B*48:44','HLA-B*48:45','HLA-B*48:46','HLA-B*48:47','HLA-B*48:48','HLA-B*49:01','HLA-B*49:02','HLA-B*49:03','HLA-B*49:04','HLA-B*49:05','HLA-B*49:06','HLA-B*49:07','HLA-B*49:08','HLA-B*49:09','HLA-B*49:10','HLA-B*49:11','HLA-B*49:12','HLA-B*49:13','HLA-B*49:14','HLA-B*49:16','HLA-B*49:17','HLA-B*49:18','HLA-B*49:20','HLA-B*49:21','HLA-B*49:22','HLA-B*49:23','HLA-B*49:24','HLA-B*49:25','HLA-B*49:26','HLA-B*49:27','HLA-B*49:28','HLA-B*49:29','HLA-B*49:30','HLA-B*49:31','HLA-B*49:32','HLA-B*49:33','HLA-B*49:34','HLA-B*49:35','HLA-B*49:36','HLA-B*49:37','HLA-B*49:38','HLA-B*49:39','HLA-B*49:40','HLA-B*49:41','HLA-B*49:42','HLA-B*49:43','HLA-B*49:44','HLA-B*49:45','HLA-B*49:46','HLA-B*49:47','HLA-B*49:48','HLA-B*49:49','HLA-B*49:50','HLA-B*49:51','HLA-B*49:52','HLA-B*49:53','HLA-B*49:54','HLA-B*49:55','HLA-B*49:56','HLA-B*49:57','HLA-B*49:58','HLA-B*49:59','HLA-B*50:01','HLA-B*50:02','HLA-B*50:04','HLA-B*50:05','HLA-B*50:06','HLA-B*50:07','HLA-B*50:08','HLA-B*50:09','HLA-B*50:10','HLA-B*50:11','HLA-B*50:12','HLA-B*50:13','HLA-B*50:14','HLA-B*50:15','HLA-B*50:16','HLA-B*50:17','HLA-B*50:18','HLA-B*50:19','HLA-B*50:20','HLA-B*50:31','HLA-B*50:32','HLA-B*50:33','HLA-B*50:34','HLA-B*50:35','HLA-B*50:36','HLA-B*50:37','HLA-B*50:38','HLA-B*50:39','HLA-B*50:40','HLA-B*50:41','HLA-B*50:42','HLA-B*50:43','HLA-B*50:44','HLA-B*50:45','HLA-B*50:46','HLA-B*50:47','HLA-B*50:48','HLA-B*50:49','HLA-B*50:50','HLA-B*50:51','HLA-B*50:52','HLA-B*50:53','HLA-B*50:54','HLA-B*50:55','HLA-B*50:56','HLA-B*50:57','HLA-B*50:58','HLA-B*50:59','HLA-B*50:60','HLA-B*50:61','HLA-B*51:01','HLA-B*51:02','HLA-B*51:03','HLA-B*51:04','HLA-B*51:05','HLA-B*51:06','HLA-B*51:07','HLA-B*51:08','HLA-B*51:09','HLA-B*51:10','HLA-B*51:100','HLA-B*51:101','HLA-B*51:102','HLA-B*51:103','HLA-B*51:104','HLA-B*51:105','HLA-B*51:106','HLA-B*51:107','HLA-B*51:108','HLA-B*51:109','HLA-B*51:111','HLA-B*51:112','HLA-B*51:113','HLA-B*51:114','HLA-B*51:115','HLA-B*51:116','HLA-B*51:117','HLA-B*51:119','HLA-B*51:12','HLA-B*51:12','HLA-B*51:120','HLA-B*51:121','HLA-B*51:122','HLA-B*51:123','HLA-B*51:124','HLA-B*51:125','HLA-B*51:126','HLA-B*51:127','HLA-B*51:128','HLA-B*51:129','HLA-B*51:13','HLA-B*51:130','HLA-B*51:131','HLA-B*51:132','HLA-B*51:133','HLA-B*51:134','HLA-B*51:135','HLA-B*51:136','HLA-B*51:137','HLA-B*51:138','HLA-B*51:139','HLA-B*51:14','HLA-B*51:140','HLA-B*51:141','HLA-B*51:142','HLA-B*51:143','HLA-B*51:144','HLA-B*51:145','HLA-B*51:146','HLA-B*51:147','HLA-B*51:148','HLA-B*51:15','HLA-B*51:150','HLA-B*51:151','HLA-B*51:152','HLA-B*51:153','HLA-B*51:154','HLA-B*51:155','HLA-B*51:156','HLA-B*51:157','HLA-B*51:158','HLA-B*51:159','HLA-B*51:16','HLA-B*51:160','HLA-B*51:161','HLA-B*51:162','HLA-B*51:163','HLA-B*51:164','HLA-B*51:165','HLA-B*51:166','HLA-B*51:167','HLA-B*51:168','HLA-B*51:169','HLA-B*51:17','HLA-B*51:170','HLA-B*51:171','HLA-B*51:172','HLA-B*51:174','HLA-B*51:175','HLA-B*51:176','HLA-B*51:177','HLA-B*51:179','HLA-B*51:18','HLA-B*51:180','HLA-B*51:181','HLA-B*51:182','HLA-B*51:183','HLA-B*51:185','HLA-B*51:186','HLA-B*51:187','HLA-B*51:188','HLA-B*51:189','HLA-B*51:19','HLA-B*51:190','HLA-B*51:191','HLA-B*51:192','HLA-B*51:193','HLA-B*51:194','HLA-B*51:195','HLA-B*51:196','HLA-B*51:197','HLA-B*51:198','HLA-B*51:199','HLA-B*51:20','HLA-B*51:200','HLA-B*51:201','HLA-B*51:202','HLA-B*51:203','HLA-B*51:204','HLA-B*51:205','HLA-B*51:206','HLA-B*51:207','HLA-B*51:208','HLA-B*51:209','HLA-B*51:21','HLA-B*51:210','HLA-B*51:211','HLA-B*51:212','HLA-B*51:213','HLA-B*51:214','HLA-B*51:215','HLA-B*51:216','HLA-B*51:217','HLA-B*51:218','HLA-B*51:219','HLA-B*51:22','HLA-B*51:220','HLA-B*51:221','HLA-B*51:222','HLA-B*51:223','HLA-B*51:224','HLA-B*51:225','HLA-B*51:226','HLA-B*51:227','HLA-B*51:228','HLA-B*51:229','HLA-B*51:23','HLA-B*51:230','HLA-B*51:231','HLA-B*51:232','HLA-B*51:233','HLA-B*51:234','HLA-B*51:236','HLA-B*51:237','HLA-B*51:238','HLA-B*51:239','HLA-B*51:24','HLA-B*51:240','HLA-B*51:241','HLA-B*51:242','HLA-B*51:243','HLA-B*51:244','HLA-B*51:246','HLA-B*51:247','HLA-B*51:248','HLA-B*51:249','HLA-B*51:250','HLA-B*51:251','HLA-B*51:252','HLA-B*51:253','HLA-B*51:254','HLA-B*51:255','HLA-B*51:257','HLA-B*51:258','HLA-B*51:259','HLA-B*51:26','HLA-B*51:260','HLA-B*51:261','HLA-B*51:262','HLA-B*51:263','HLA-B*51:265','HLA-B*51:266','HLA-B*51:267','HLA-B*51:28','HLA-B*51:29','HLA-B*51:30','HLA-B*51:31','HLA-B*51:32','HLA-B*51:33','HLA-B*51:34','HLA-B*51:35','HLA-B*51:36','HLA-B*51:37','HLA-B*51:38','HLA-B*51:39','HLA-B*51:40','HLA-B*51:42','HLA-B*51:43','HLA-B*51:45','HLA-B*51:46','HLA-B*51:48','HLA-B*51:49','HLA-B*51:50','HLA-B*51:51','HLA-B*51:52','HLA-B*51:53','HLA-B*51:54','HLA-B*51:55','HLA-B*51:56','HLA-B*51:57','HLA-B*51:58','HLA-B*51:59','HLA-B*51:60','HLA-B*51:61','HLA-B*51:62','HLA-B*51:63','HLA-B*51:64','HLA-B*51:65','HLA-B*51:66','HLA-B*51:67','HLA-B*51:68','HLA-B*51:69','HLA-B*51:70','HLA-B*51:71','HLA-B*51:72','HLA-B*51:73','HLA-B*51:74','HLA-B*51:75','HLA-B*51:76','HLA-B*51:77','HLA-B*51:78','HLA-B*51:79','HLA-B*51:80','HLA-B*51:81','HLA-B*51:82','HLA-B*51:83','HLA-B*51:84','HLA-B*51:85','HLA-B*51:86','HLA-B*51:87','HLA-B*51:88','HLA-B*51:89','HLA-B*51:90','HLA-B*51:91','HLA-B*51:92','HLA-B*51:93','HLA-B*51:94','HLA-B*51:95','HLA-B*51:96','HLA-B*51:97','HLA-B*51:99','HLA-B*52:01','HLA-B*52:02','HLA-B*52:03','HLA-B*52:04','HLA-B*52:05','HLA-B*52:06','HLA-B*52:07','HLA-B*52:08','HLA-B*52:09','HLA-B*52:10','HLA-B*52:11','HLA-B*52:12','HLA-B*52:13','HLA-B*52:14','HLA-B*52:15','HLA-B*52:16','HLA-B*52:17','HLA-B*52:18','HLA-B*52:19','HLA-B*52:20','HLA-B*52:21','HLA-B*52:22','HLA-B*52:23','HLA-B*52:24','HLA-B*52:25','HLA-B*52:26','HLA-B*52:27','HLA-B*52:28','HLA-B*52:29','HLA-B*52:30','HLA-B*52:31','HLA-B*52:32','HLA-B*52:33','HLA-B*52:34','HLA-B*52:35','HLA-B*52:36','HLA-B*52:37','HLA-B*52:38','HLA-B*52:39','HLA-B*52:40','HLA-B*52:41','HLA-B*52:42','HLA-B*52:43','HLA-B*52:44','HLA-B*52:45','HLA-B*52:46','HLA-B*52:47','HLA-B*52:48','HLA-B*52:50','HLA-B*52:51','HLA-B*52:52','HLA-B*52:53','HLA-B*52:54','HLA-B*52:55','HLA-B*52:56','HLA-B*52:57','HLA-B*52:58','HLA-B*52:59','HLA-B*52:60','HLA-B*52:61','HLA-B*52:62','HLA-B*52:63','HLA-B*52:64','HLA-B*52:65','HLA-B*52:66','HLA-B*52:67','HLA-B*52:68','HLA-B*52:69','HLA-B*52:70','HLA-B*52:71','HLA-B*52:72','HLA-B*52:73','HLA-B*52:74','HLA-B*52:75','HLA-B*52:76','HLA-B*52:77','HLA-B*52:78','HLA-B*52:79','HLA-B*52:80','HLA-B*52:81','HLA-B*52:82','HLA-B*52:83','HLA-B*52:84','HLA-B*53:01','HLA-B*53:02','HLA-B*53:03','HLA-B*53:04','HLA-B*53:05','HLA-B*53:06','HLA-B*53:07','HLA-B*53:08','HLA-B*53:09','HLA-B*53:10','HLA-B*53:11','HLA-B*53:12','HLA-B*53:13','HLA-B*53:14','HLA-B*53:15','HLA-B*53:16','HLA-B*53:17','HLA-B*53:18','HLA-B*53:19','HLA-B*53:20','HLA-B*53:21','HLA-B*53:22','HLA-B*53:23','HLA-B*53:24','HLA-B*53:25','HLA-B*53:26','HLA-B*53:27','HLA-B*53:28','HLA-B*53:29','HLA-B*53:30','HLA-B*53:31','HLA-B*53:32','HLA-B*53:33','HLA-B*53:34','HLA-B*53:35','HLA-B*53:36','HLA-B*53:37','HLA-B*53:38','HLA-B*53:39','HLA-B*53:40','HLA-B*53:41','HLA-B*53:42','HLA-B*53:43','HLA-B*53:44','HLA-B*53:45','HLA-B*53:46','HLA-B*53:47','HLA-B*53:49','HLA-B*53:50','HLA-B*53:51','HLA-B*53:52','HLA-B*53:53','HLA-B*54:01','HLA-B*54:02','HLA-B*54:03','HLA-B*54:04','HLA-B*54:06','HLA-B*54:07','HLA-B*54:09','HLA-B*54:10','HLA-B*54:11','HLA-B*54:12','HLA-B*54:13','HLA-B*54:14','HLA-B*54:15','HLA-B*54:16','HLA-B*54:17','HLA-B*54:18','HLA-B*54:19','HLA-B*54:20','HLA-B*54:21','HLA-B*54:22','HLA-B*54:23','HLA-B*54:24','HLA-B*54:25','HLA-B*54:26','HLA-B*54:27','HLA-B*54:28','HLA-B*54:29','HLA-B*54:30','HLA-B*54:31','HLA-B*54:32','HLA-B*54:33','HLA-B*54:34','HLA-B*54:35','HLA-B*54:36','HLA-B*54:37','HLA-B*54:38','HLA-B*55:01','HLA-B*55:02','HLA-B*55:03','HLA-B*55:04','HLA-B*55:05','HLA-B*55:07','HLA-B*55:08','HLA-B*55:09','HLA-B*55:10','HLA-B*55:11','HLA-B*55:12','HLA-B*55:13','HLA-B*55:14','HLA-B*55:15','HLA-B*55:16','HLA-B*55:17','HLA-B*55:18','HLA-B*55:19','HLA-B*55:20','HLA-B*55:21','HLA-B*55:22','HLA-B*55:23','HLA-B*55:24','HLA-B*55:25','HLA-B*55:26','HLA-B*55:27','HLA-B*55:28','HLA-B*55:29','HLA-B*55:30','HLA-B*55:31','HLA-B*55:32','HLA-B*55:33','HLA-B*55:34','HLA-B*55:35','HLA-B*55:36','HLA-B*55:37','HLA-B*55:38','HLA-B*55:39','HLA-B*55:40','HLA-B*55:41','HLA-B*55:42','HLA-B*55:43','HLA-B*55:44','HLA-B*55:45','HLA-B*55:46','HLA-B*55:47','HLA-B*55:48','HLA-B*55:49','HLA-B*55:50','HLA-B*55:51','HLA-B*55:52','HLA-B*55:53','HLA-B*55:54','HLA-B*55:56','HLA-B*55:57','HLA-B*55:58','HLA-B*55:59','HLA-B*55:60','HLA-B*55:61','HLA-B*55:62','HLA-B*55:63','HLA-B*55:64','HLA-B*55:65','HLA-B*55:66','HLA-B*55:67','HLA-B*55:68','HLA-B*55:69','HLA-B*55:70','HLA-B*55:71','HLA-B*55:72','HLA-B*55:73','HLA-B*55:74','HLA-B*55:75','HLA-B*55:76','HLA-B*55:77','HLA-B*55:78','HLA-B*55:79','HLA-B*55:80','HLA-B*55:81','HLA-B*55:82','HLA-B*55:84','HLA-B*55:85','HLA-B*55:86','HLA-B*55:87','HLA-B*55:88','HLA-B*55:90','HLA-B*55:91','HLA-B*55:92','HLA-B*55:93','HLA-B*55:94','HLA-B*55:95','HLA-B*55:96','HLA-B*56:01','HLA-B*56:02','HLA-B*56:03','HLA-B*56:04','HLA-B*56:05','HLA-B*56:06','HLA-B*56:07','HLA-B*56:08','HLA-B*56:09','HLA-B*56:10','HLA-B*56:11','HLA-B*56:12','HLA-B*56:13','HLA-B*56:14','HLA-B*56:15','HLA-B*56:16','HLA-B*56:17','HLA-B*56:18','HLA-B*56:20','HLA-B*56:21','HLA-B*56:22','HLA-B*56:23','HLA-B*56:24','HLA-B*56:25','HLA-B*56:26','HLA-B*56:27','HLA-B*56:29','HLA-B*56:30','HLA-B*56:31','HLA-B*56:32','HLA-B*56:33','HLA-B*56:34','HLA-B*56:35','HLA-B*56:36','HLA-B*56:37','HLA-B*56:39','HLA-B*56:40','HLA-B*56:41','HLA-B*56:42','HLA-B*56:43','HLA-B*56:44','HLA-B*56:45','HLA-B*56:46','HLA-B*56:47','HLA-B*56:48','HLA-B*56:49','HLA-B*56:50','HLA-B*56:51','HLA-B*56:52','HLA-B*56:53','HLA-B*56:54','HLA-B*56:55','HLA-B*56:56','HLA-B*56:57','HLA-B*56:58','HLA-B*56:59','HLA-B*56:60','HLA-B*56:61','HLA-B*56:62','HLA-B*56:63','HLA-B*56:64','HLA-B*57:01','HLA-B*57:02','HLA-B*57:03','HLA-B*57:04','HLA-B*57:05','HLA-B*57:06','HLA-B*57:07','HLA-B*57:08','HLA-B*57:09','HLA-B*57:10','HLA-B*57:100','HLA-B*57:101','HLA-B*57:102','HLA-B*57:103','HLA-B*57:104','HLA-B*57:105','HLA-B*57:106','HLA-B*57:107','HLA-B*57:108','HLA-B*57:109','HLA-B*57:11','HLA-B*57:110','HLA-B*57:111','HLA-B*57:112','HLA-B*57:113','HLA-B*57:114','HLA-B*57:12','HLA-B*57:13','HLA-B*57:14','HLA-B*57:15','HLA-B*57:16','HLA-B*57:17','HLA-B*57:18','HLA-B*57:19','HLA-B*57:20','HLA-B*57:21','HLA-B*57:22','HLA-B*57:23','HLA-B*57:24','HLA-B*57:25','HLA-B*57:26','HLA-B*57:27','HLA-B*57:29','HLA-B*57:30','HLA-B*57:31','HLA-B*57:32','HLA-B*57:33','HLA-B*57:34','HLA-B*57:35','HLA-B*57:36','HLA-B*57:37','HLA-B*57:38','HLA-B*57:39','HLA-B*57:40','HLA-B*57:41','HLA-B*57:42','HLA-B*57:43','HLA-B*57:44','HLA-B*57:45','HLA-B*57:46','HLA-B*57:47','HLA-B*57:48','HLA-B*57:49','HLA-B*57:50','HLA-B*57:51','HLA-B*57:52','HLA-B*57:53','HLA-B*57:54','HLA-B*57:55','HLA-B*57:56','HLA-B*57:57','HLA-B*57:58','HLA-B*57:59','HLA-B*57:60','HLA-B*57:61','HLA-B*57:62','HLA-B*57:63','HLA-B*57:64','HLA-B*57:65','HLA-B*57:66','HLA-B*57:67','HLA-B*57:68','HLA-B*57:69','HLA-B*57:70','HLA-B*57:71','HLA-B*57:72','HLA-B*57:73','HLA-B*57:74','HLA-B*57:75','HLA-B*57:76','HLA-B*57:77','HLA-B*57:78','HLA-B*57:80','HLA-B*57:81','HLA-B*57:82','HLA-B*57:83','HLA-B*57:84','HLA-B*57:85','HLA-B*57:86','HLA-B*57:87','HLA-B*57:88','HLA-B*57:89','HLA-B*57:90','HLA-B*57:91','HLA-B*57:92','HLA-B*57:93','HLA-B*57:94','HLA-B*57:95','HLA-B*57:96','HLA-B*57:97','HLA-B*57:99','HLA-B*58:01','HLA-B*58:02','HLA-B*58:04','HLA-B*58:05','HLA-B*58:06','HLA-B*58:07','HLA-B*58:08','HLA-B*58:09','HLA-B*58:100','HLA-B*58:11','HLA-B*58:12','HLA-B*58:13','HLA-B*58:14','HLA-B*58:15','HLA-B*58:16','HLA-B*58:18','HLA-B*58:19','HLA-B*58:20','HLA-B*58:21','HLA-B*58:22','HLA-B*58:23','HLA-B*58:24','HLA-B*58:25','HLA-B*58:26','HLA-B*58:27','HLA-B*58:28','HLA-B*58:29','HLA-B*58:30','HLA-B*58:32','HLA-B*58:33','HLA-B*58:34','HLA-B*58:35','HLA-B*58:36','HLA-B*58:37','HLA-B*58:38','HLA-B*58:40','HLA-B*58:41','HLA-B*58:42','HLA-B*58:43','HLA-B*58:44','HLA-B*58:45','HLA-B*58:46','HLA-B*58:47','HLA-B*58:48','HLA-B*58:49','HLA-B*58:50','HLA-B*58:51','HLA-B*58:52','HLA-B*58:53','HLA-B*58:54','HLA-B*58:55','HLA-B*58:56','HLA-B*58:57','HLA-B*58:58','HLA-B*58:59','HLA-B*58:60','HLA-B*58:61','HLA-B*58:62','HLA-B*58:63','HLA-B*58:64','HLA-B*58:65','HLA-B*58:66','HLA-B*58:67','HLA-B*58:68','HLA-B*58:69','HLA-B*58:70','HLA-B*58:71','HLA-B*58:73','HLA-B*58:74','HLA-B*58:75','HLA-B*58:76','HLA-B*58:77','HLA-B*58:78','HLA-B*58:79','HLA-B*58:80','HLA-B*58:81','HLA-B*58:82','HLA-B*58:83','HLA-B*58:84','HLA-B*58:85','HLA-B*58:86','HLA-B*58:87','HLA-B*58:88','HLA-B*58:89','HLA-B*58:90','HLA-B*58:91','HLA-B*58:92','HLA-B*58:95','HLA-B*58:96','HLA-B*58:97','HLA-B*58:98','HLA-B*58:99','HLA-B*59:01','HLA-B*59:02','HLA-B*59:03','HLA-B*59:04','HLA-B*59:05','HLA-B*59:06','HLA-B*59:07','HLA-B*59:08','HLA-B*59:09','HLA-B*67:01','HLA-B*67:02','HLA-B*67:03','HLA-B*67:04','HLA-B*67:05','HLA-B*67:06','HLA-B*67:07','HLA-B*73:01','HLA-B*73:02','HLA-B*78:01','HLA-B*78:02','HLA-B*78:03','HLA-B*78:04','HLA-B*78:05','HLA-B*78:06','HLA-B*78:07','HLA-B*78:08','HLA-B*78:09','HLA-B*78:10','HLA-B*81:01','HLA-B*81:02','HLA-B*81:03','HLA-B*81:05','HLA-B*81:06','HLA-B*81:07','HLA-B*81:08','HLA-B*82:01','HLA-B*82:02','HLA-B*82:03','HLA-B*83:01','HLA-C*01:02','HLA-C*01:03','HLA-C*01:04','HLA-C*01:05','HLA-C*01:06','HLA-C*01:07','HLA-C*01:08','HLA-C*01:09','HLA-C*01:10','HLA-C*01:100','HLA-C*01:101','HLA-C*01:102','HLA-C*01:103','HLA-C*01:104','HLA-C*01:105','HLA-C*01:106','HLA-C*01:107','HLA-C*01:108','HLA-C*01:11','HLA-C*01:110','HLA-C*01:112','HLA-C*01:113','HLA-C*01:114','HLA-C*01:115','HLA-C*01:116','HLA-C*01:118','HLA-C*01:119','HLA-C*01:12','HLA-C*01:120','HLA-C*01:122','HLA-C*01:123','HLA-C*01:124','HLA-C*01:125','HLA-C*01:126','HLA-C*01:127','HLA-C*01:128','HLA-C*01:129','HLA-C*01:13','HLA-C*01:130','HLA-C*01:131','HLA-C*01:132','HLA-C*01:133','HLA-C*01:134','HLA-C*01:135','HLA-C*01:136','HLA-C*01:138','HLA-C*01:139','HLA-C*01:14','HLA-C*01:140','HLA-C*01:141','HLA-C*01:142','HLA-C*01:144','HLA-C*01:146','HLA-C*01:147','HLA-C*01:148','HLA-C*01:149','HLA-C*01:15','HLA-C*01:150','HLA-C*01:151','HLA-C*01:152','HLA-C*01:153','HLA-C*01:154','HLA-C*01:155','HLA-C*01:156','HLA-C*01:157','HLA-C*01:158','HLA-C*01:159','HLA-C*01:16','HLA-C*01:160','HLA-C*01:161','HLA-C*01:162','HLA-C*01:163','HLA-C*01:164','HLA-C*01:165','HLA-C*01:166','HLA-C*01:167','HLA-C*01:168','HLA-C*01:169','HLA-C*01:17','HLA-C*01:170','HLA-C*01:172','HLA-C*01:173','HLA-C*01:174','HLA-C*01:175','HLA-C*01:176','HLA-C*01:18','HLA-C*01:19','HLA-C*01:20','HLA-C*01:21','HLA-C*01:22','HLA-C*01:23','HLA-C*01:24','HLA-C*01:25','HLA-C*01:26','HLA-C*01:27','HLA-C*01:28','HLA-C*01:29','HLA-C*01:30','HLA-C*01:31','HLA-C*01:32','HLA-C*01:33','HLA-C*01:34','HLA-C*01:35','HLA-C*01:36','HLA-C*01:38','HLA-C*01:39','HLA-C*01:40','HLA-C*01:41','HLA-C*01:42','HLA-C*01:43','HLA-C*01:44','HLA-C*01:45','HLA-C*01:46','HLA-C*01:47','HLA-C*01:48','HLA-C*01:49','HLA-C*01:50','HLA-C*01:51','HLA-C*01:52','HLA-C*01:53','HLA-C*01:54','HLA-C*01:55','HLA-C*01:57','HLA-C*01:58','HLA-C*01:59','HLA-C*01:60','HLA-C*01:61','HLA-C*01:62','HLA-C*01:63','HLA-C*01:64','HLA-C*01:65','HLA-C*01:66','HLA-C*01:67','HLA-C*01:68','HLA-C*01:70','HLA-C*01:71','HLA-C*01:72','HLA-C*01:73','HLA-C*01:74','HLA-C*01:75','HLA-C*01:76','HLA-C*01:77','HLA-C*01:78','HLA-C*01:79','HLA-C*01:80','HLA-C*01:81','HLA-C*01:82','HLA-C*01:83','HLA-C*01:84','HLA-C*01:85','HLA-C*01:87','HLA-C*01:88','HLA-C*01:90','HLA-C*01:91','HLA-C*01:92','HLA-C*01:93','HLA-C*01:94','HLA-C*01:95','HLA-C*01:96','HLA-C*01:97','HLA-C*01:99','HLA-C*02:02','HLA-C*02:03','HLA-C*02:04','HLA-C*02:05','HLA-C*02:06','HLA-C*02:07','HLA-C*02:08','HLA-C*02:09','HLA-C*02:10','HLA-C*02:100','HLA-C*02:101','HLA-C*02:102','HLA-C*02:103','HLA-C*02:104','HLA-C*02:106','HLA-C*02:107','HLA-C*02:108','HLA-C*02:109','HLA-C*02:11','HLA-C*02:110','HLA-C*02:111','HLA-C*02:112','HLA-C*02:113','HLA-C*02:114','HLA-C*02:115','HLA-C*02:116','HLA-C*02:117','HLA-C*02:118','HLA-C*02:119','HLA-C*02:12','HLA-C*02:120','HLA-C*02:122','HLA-C*02:123','HLA-C*02:124','HLA-C*02:125','HLA-C*02:126','HLA-C*02:127','HLA-C*02:128','HLA-C*02:129','HLA-C*02:13','HLA-C*02:130','HLA-C*02:131','HLA-C*02:132','HLA-C*02:133','HLA-C*02:134','HLA-C*02:136','HLA-C*02:137','HLA-C*02:138','HLA-C*02:139','HLA-C*02:14','HLA-C*02:140','HLA-C*02:141','HLA-C*02:142','HLA-C*02:143','HLA-C*02:144','HLA-C*02:145','HLA-C*02:146','HLA-C*02:147','HLA-C*02:148','HLA-C*02:149','HLA-C*02:15','HLA-C*02:151','HLA-C*02:152','HLA-C*02:153','HLA-C*02:154','HLA-C*02:155','HLA-C*02:156','HLA-C*02:157','HLA-C*02:158','HLA-C*02:159','HLA-C*02:16','HLA-C*02:160','HLA-C*02:161','HLA-C*02:162','HLA-C*02:163','HLA-C*02:164','HLA-C*02:166','HLA-C*02:17','HLA-C*02:18','HLA-C*02:19','HLA-C*02:20','HLA-C*02:21','HLA-C*02:22','HLA-C*02:23','HLA-C*02:24','HLA-C*02:26','HLA-C*02:27','HLA-C*02:28','HLA-C*02:29','HLA-C*02:30','HLA-C*02:31','HLA-C*02:32','HLA-C*02:33','HLA-C*02:34','HLA-C*02:35','HLA-C*02:36','HLA-C*02:37','HLA-C*02:39','HLA-C*02:40','HLA-C*02:42','HLA-C*02:43','HLA-C*02:44','HLA-C*02:45','HLA-C*02:46','HLA-C*02:47','HLA-C*02:48','HLA-C*02:49','HLA-C*02:50','HLA-C*02:51','HLA-C*02:53','HLA-C*02:54','HLA-C*02:55','HLA-C*02:56','HLA-C*02:57','HLA-C*02:58','HLA-C*02:59','HLA-C*02:60','HLA-C*02:61','HLA-C*02:62','HLA-C*02:63','HLA-C*02:64','HLA-C*02:65','HLA-C*02:66','HLA-C*02:68','HLA-C*02:69','HLA-C*02:70','HLA-C*02:71','HLA-C*02:72','HLA-C*02:73','HLA-C*02:74','HLA-C*02:75','HLA-C*02:76','HLA-C*02:77','HLA-C*02:78','HLA-C*02:79','HLA-C*02:80','HLA-C*02:81','HLA-C*02:82','HLA-C*02:83','HLA-C*02:84','HLA-C*02:85','HLA-C*02:86','HLA-C*02:87','HLA-C*02:88','HLA-C*02:89','HLA-C*02:90','HLA-C*02:91','HLA-C*02:93','HLA-C*02:94','HLA-C*02:95','HLA-C*02:96','HLA-C*02:97','HLA-C*02:98','HLA-C*02:99','HLA-C*03:01','HLA-C*03:02','HLA-C*03:03','HLA-C*03:04','HLA-C*03:05','HLA-C*03:06','HLA-C*03:07','HLA-C*03:08','HLA-C*03:09','HLA-C*03:10','HLA-C*03:100','HLA-C*03:101','HLA-C*03:102','HLA-C*03:103','HLA-C*03:104','HLA-C*03:105','HLA-C*03:106','HLA-C*03:107','HLA-C*03:108','HLA-C*03:109','HLA-C*03:11','HLA-C*03:110','HLA-C*03:111','HLA-C*03:112','HLA-C*03:113','HLA-C*03:114','HLA-C*03:115','HLA-C*03:116','HLA-C*03:117','HLA-C*03:118','HLA-C*03:119','HLA-C*03:12','HLA-C*03:120','HLA-C*03:122','HLA-C*03:123','HLA-C*03:124','HLA-C*03:125','HLA-C*03:126','HLA-C*03:127','HLA-C*03:128','HLA-C*03:129','HLA-C*03:13','HLA-C*03:130','HLA-C*03:131','HLA-C*03:132','HLA-C*03:133','HLA-C*03:134','HLA-C*03:135','HLA-C*03:136','HLA-C*03:137','HLA-C*03:138','HLA-C*03:139','HLA-C*03:14','HLA-C*03:140','HLA-C*03:141','HLA-C*03:142','HLA-C*03:143','HLA-C*03:144','HLA-C*03:145','HLA-C*03:146','HLA-C*03:147','HLA-C*03:148','HLA-C*03:149','HLA-C*03:15','HLA-C*03:150','HLA-C*03:151','HLA-C*03:152','HLA-C*03:153','HLA-C*03:154','HLA-C*03:155','HLA-C*03:156','HLA-C*03:157','HLA-C*03:158','HLA-C*03:159','HLA-C*03:16','HLA-C*03:160','HLA-C*03:161','HLA-C*03:162','HLA-C*03:163','HLA-C*03:164','HLA-C*03:165','HLA-C*03:166','HLA-C*03:167','HLA-C*03:168','HLA-C*03:17','HLA-C*03:170','HLA-C*03:171','HLA-C*03:172','HLA-C*03:173','HLA-C*03:174','HLA-C*03:175','HLA-C*03:176','HLA-C*03:177','HLA-C*03:178','HLA-C*03:179','HLA-C*03:18','HLA-C*03:180','HLA-C*03:181','HLA-C*03:182','HLA-C*03:183','HLA-C*03:184','HLA-C*03:185','HLA-C*03:186','HLA-C*03:187','HLA-C*03:188','HLA-C*03:19','HLA-C*03:190','HLA-C*03:191','HLA-C*03:192','HLA-C*03:193','HLA-C*03:194','HLA-C*03:195','HLA-C*03:196','HLA-C*03:197','HLA-C*03:198','HLA-C*03:199','HLA-C*03:200','HLA-C*03:202','HLA-C*03:203','HLA-C*03:204','HLA-C*03:205','HLA-C*03:206','HLA-C*03:207','HLA-C*03:209','HLA-C*03:21','HLA-C*03:210','HLA-C*03:211','HLA-C*03:212','HLA-C*03:213','HLA-C*03:214','HLA-C*03:215','HLA-C*03:216','HLA-C*03:217','HLA-C*03:218','HLA-C*03:219','HLA-C*03:220','HLA-C*03:221','HLA-C*03:222','HLA-C*03:223','HLA-C*03:225','HLA-C*03:226','HLA-C*03:227','HLA-C*03:228','HLA-C*03:23','HLA-C*03:230','HLA-C*03:231','HLA-C*03:232','HLA-C*03:233','HLA-C*03:234','HLA-C*03:235','HLA-C*03:236','HLA-C*03:237','HLA-C*03:238','HLA-C*03:239','HLA-C*03:24','HLA-C*03:240','HLA-C*03:241','HLA-C*03:242','HLA-C*03:243','HLA-C*03:245','HLA-C*03:246','HLA-C*03:247','HLA-C*03:248','HLA-C*03:249','HLA-C*03:25','HLA-C*03:250','HLA-C*03:251','HLA-C*03:252','HLA-C*03:253','HLA-C*03:254','HLA-C*03:255','HLA-C*03:256','HLA-C*03:257','HLA-C*03:258','HLA-C*03:259','HLA-C*03:26','HLA-C*03:260','HLA-C*03:261','HLA-C*03:262','HLA-C*03:263','HLA-C*03:264','HLA-C*03:266','HLA-C*03:267','HLA-C*03:268','HLA-C*03:269','HLA-C*03:27','HLA-C*03:270','HLA-C*03:271','HLA-C*03:272','HLA-C*03:273','HLA-C*03:274','HLA-C*03:275','HLA-C*03:276','HLA-C*03:278','HLA-C*03:279','HLA-C*03:28','HLA-C*03:280','HLA-C*03:281','HLA-C*03:282','HLA-C*03:283','HLA-C*03:284','HLA-C*03:285','HLA-C*03:286','HLA-C*03:287','HLA-C*03:288','HLA-C*03:289','HLA-C*03:29','HLA-C*03:290','HLA-C*03:291','HLA-C*03:292','HLA-C*03:293','HLA-C*03:294','HLA-C*03:295','HLA-C*03:296','HLA-C*03:297','HLA-C*03:298','HLA-C*03:299','HLA-C*03:30','HLA-C*03:300','HLA-C*03:301','HLA-C*03:302','HLA-C*03:303','HLA-C*03:304','HLA-C*03:305','HLA-C*03:306','HLA-C*03:307','HLA-C*03:308','HLA-C*03:309','HLA-C*03:31','HLA-C*03:310','HLA-C*03:311','HLA-C*03:312','HLA-C*03:313','HLA-C*03:314','HLA-C*03:315','HLA-C*03:317','HLA-C*03:319','HLA-C*03:32','HLA-C*03:320','HLA-C*03:321','HLA-C*03:322','HLA-C*03:324','HLA-C*03:325','HLA-C*03:326','HLA-C*03:327','HLA-C*03:328','HLA-C*03:329','HLA-C*03:33','HLA-C*03:330','HLA-C*03:331','HLA-C*03:332','HLA-C*03:333','HLA-C*03:334','HLA-C*03:335','HLA-C*03:336','HLA-C*03:337','HLA-C*03:338','HLA-C*03:339','HLA-C*03:34','HLA-C*03:340','HLA-C*03:341','HLA-C*03:342','HLA-C*03:343','HLA-C*03:344','HLA-C*03:345','HLA-C*03:346','HLA-C*03:347','HLA-C*03:348','HLA-C*03:349','HLA-C*03:35','HLA-C*03:350','HLA-C*03:351','HLA-C*03:352','HLA-C*03:353','HLA-C*03:354','HLA-C*03:355','HLA-C*03:356','HLA-C*03:357','HLA-C*03:358','HLA-C*03:359','HLA-C*03:36','HLA-C*03:360','HLA-C*03:361','HLA-C*03:362','HLA-C*03:364','HLA-C*03:365','HLA-C*03:367','HLA-C*03:368','HLA-C*03:369','HLA-C*03:37','HLA-C*03:370','HLA-C*03:371','HLA-C*03:372','HLA-C*03:373','HLA-C*03:374','HLA-C*03:375','HLA-C*03:376','HLA-C*03:378','HLA-C*03:379','HLA-C*03:38','HLA-C*03:381','HLA-C*03:382','HLA-C*03:383','HLA-C*03:384','HLA-C*03:385','HLA-C*03:386','HLA-C*03:387','HLA-C*03:388','HLA-C*03:389','HLA-C*03:39','HLA-C*03:390','HLA-C*03:393','HLA-C*03:394','HLA-C*03:395','HLA-C*03:397','HLA-C*03:398','HLA-C*03:399','HLA-C*03:40','HLA-C*03:400','HLA-C*03:401','HLA-C*03:402','HLA-C*03:403','HLA-C*03:404','HLA-C*03:405','HLA-C*03:406','HLA-C*03:407','HLA-C*03:408','HLA-C*03:409','HLA-C*03:41','HLA-C*03:410','HLA-C*03:411','HLA-C*03:412','HLA-C*03:413','HLA-C*03:414','HLA-C*03:415','HLA-C*03:416','HLA-C*03:417','HLA-C*03:418','HLA-C*03:419','HLA-C*03:42','HLA-C*03:420','HLA-C*03:422','HLA-C*03:423','HLA-C*03:425','HLA-C*03:426','HLA-C*03:427','HLA-C*03:428','HLA-C*03:429','HLA-C*03:43','HLA-C*03:430','HLA-C*03:431','HLA-C*03:433','HLA-C*03:434','HLA-C*03:435','HLA-C*03:436','HLA-C*03:437','HLA-C*03:438','HLA-C*03:439','HLA-C*03:44','HLA-C*03:440','HLA-C*03:441','HLA-C*03:443','HLA-C*03:45','HLA-C*03:450','HLA-C*03:451','HLA-C*03:452','HLA-C*03:453','HLA-C*03:454','HLA-C*03:455','HLA-C*03:456','HLA-C*03:457','HLA-C*03:458','HLA-C*03:459','HLA-C*03:46','HLA-C*03:460','HLA-C*03:47','HLA-C*03:48','HLA-C*03:49','HLA-C*03:50','HLA-C*03:51','HLA-C*03:52','HLA-C*03:53','HLA-C*03:54','HLA-C*03:55','HLA-C*03:56','HLA-C*03:57','HLA-C*03:58','HLA-C*03:59','HLA-C*03:60','HLA-C*03:61','HLA-C*03:62','HLA-C*03:63','HLA-C*03:64','HLA-C*03:65','HLA-C*03:66','HLA-C*03:67','HLA-C*03:68','HLA-C*03:69','HLA-C*03:70','HLA-C*03:71','HLA-C*03:72','HLA-C*03:73','HLA-C*03:74','HLA-C*03:75','HLA-C*03:76','HLA-C*03:77','HLA-C*03:78','HLA-C*03:79','HLA-C*03:80','HLA-C*03:81','HLA-C*03:82','HLA-C*03:83','HLA-C*03:84','HLA-C*03:85','HLA-C*03:86','HLA-C*03:87','HLA-C*03:88','HLA-C*03:89','HLA-C*03:90','HLA-C*03:91','HLA-C*03:92','HLA-C*03:93','HLA-C*03:94','HLA-C*03:95','HLA-C*03:96','HLA-C*03:97','HLA-C*03:98','HLA-C*03:99','HLA-C*04:01','HLA-C*04:03','HLA-C*04:04','HLA-C*04:05','HLA-C*04:06','HLA-C*04:07','HLA-C*04:08','HLA-C*04:10','HLA-C*04:100','HLA-C*04:101','HLA-C*04:102','HLA-C*04:103','HLA-C*04:104','HLA-C*04:106','HLA-C*04:107','HLA-C*04:108','HLA-C*04:109','HLA-C*04:11','HLA-C*04:110','HLA-C*04:111','HLA-C*04:112','HLA-C*04:113','HLA-C*04:114','HLA-C*04:116','HLA-C*04:117','HLA-C*04:118','HLA-C*04:119','HLA-C*04:12','HLA-C*04:120','HLA-C*04:121','HLA-C*04:122','HLA-C*04:124','HLA-C*04:125','HLA-C*04:126','HLA-C*04:127','HLA-C*04:128','HLA-C*04:129','HLA-C*04:13','HLA-C*04:130','HLA-C*04:131','HLA-C*04:132','HLA-C*04:133','HLA-C*04:134','HLA-C*04:135','HLA-C*04:136','HLA-C*04:137','HLA-C*04:138','HLA-C*04:139','HLA-C*04:14','HLA-C*04:140','HLA-C*04:141','HLA-C*04:142','HLA-C*04:143','HLA-C*04:144','HLA-C*04:145','HLA-C*04:146','HLA-C*04:147','HLA-C*04:148','HLA-C*04:149','HLA-C*04:15','HLA-C*04:150','HLA-C*04:151','HLA-C*04:152','HLA-C*04:153','HLA-C*04:154','HLA-C*04:155','HLA-C*04:156','HLA-C*04:157','HLA-C*04:158','HLA-C*04:159','HLA-C*04:16','HLA-C*04:160','HLA-C*04:161','HLA-C*04:162','HLA-C*04:163','HLA-C*04:164','HLA-C*04:165','HLA-C*04:166','HLA-C*04:167','HLA-C*04:168','HLA-C*04:169','HLA-C*04:17','HLA-C*04:171','HLA-C*04:172','HLA-C*04:174','HLA-C*04:175','HLA-C*04:176','HLA-C*04:177','HLA-C*04:178','HLA-C*04:179','HLA-C*04:18','HLA-C*04:180','HLA-C*04:181','HLA-C*04:182','HLA-C*04:183','HLA-C*04:184','HLA-C*04:185','HLA-C*04:186','HLA-C*04:187','HLA-C*04:188','HLA-C*04:189','HLA-C*04:19','HLA-C*04:190','HLA-C*04:192','HLA-C*04:193','HLA-C*04:194','HLA-C*04:195','HLA-C*04:196','HLA-C*04:197','HLA-C*04:198','HLA-C*04:199','HLA-C*04:20','HLA-C*04:200','HLA-C*04:201','HLA-C*04:202','HLA-C*04:204','HLA-C*04:206','HLA-C*04:207','HLA-C*04:208','HLA-C*04:209','HLA-C*04:210','HLA-C*04:211','HLA-C*04:212','HLA-C*04:213','HLA-C*04:214','HLA-C*04:216','HLA-C*04:218','HLA-C*04:219','HLA-C*04:220','HLA-C*04:221','HLA-C*04:222','HLA-C*04:223','HLA-C*04:224','HLA-C*04:226','HLA-C*04:227','HLA-C*04:228','HLA-C*04:229','HLA-C*04:23','HLA-C*04:230','HLA-C*04:231','HLA-C*04:232','HLA-C*04:235','HLA-C*04:237','HLA-C*04:238','HLA-C*04:239','HLA-C*04:24','HLA-C*04:240','HLA-C*04:241','HLA-C*04:242','HLA-C*04:243','HLA-C*04:244','HLA-C*04:245','HLA-C*04:246','HLA-C*04:247','HLA-C*04:248','HLA-C*04:249','HLA-C*04:25','HLA-C*04:250','HLA-C*04:251','HLA-C*04:252','HLA-C*04:254','HLA-C*04:256','HLA-C*04:257','HLA-C*04:258','HLA-C*04:259','HLA-C*04:26','HLA-C*04:260','HLA-C*04:261','HLA-C*04:262','HLA-C*04:263','HLA-C*04:264','HLA-C*04:265','HLA-C*04:266','HLA-C*04:267','HLA-C*04:268','HLA-C*04:269','HLA-C*04:27','HLA-C*04:270','HLA-C*04:271','HLA-C*04:272','HLA-C*04:273','HLA-C*04:274','HLA-C*04:275','HLA-C*04:276','HLA-C*04:277','HLA-C*04:278','HLA-C*04:28','HLA-C*04:280','HLA-C*04:281','HLA-C*04:282','HLA-C*04:283','HLA-C*04:284','HLA-C*04:285','HLA-C*04:286','HLA-C*04:287','HLA-C*04:288','HLA-C*04:289','HLA-C*04:29','HLA-C*04:290','HLA-C*04:291','HLA-C*04:292','HLA-C*04:293','HLA-C*04:294','HLA-C*04:295','HLA-C*04:296','HLA-C*04:297','HLA-C*04:298','HLA-C*04:299','HLA-C*04:30','HLA-C*04:301','HLA-C*04:302','HLA-C*04:303','HLA-C*04:304','HLA-C*04:306','HLA-C*04:307','HLA-C*04:308','HLA-C*04:31','HLA-C*04:310','HLA-C*04:311','HLA-C*04:312','HLA-C*04:313','HLA-C*04:314','HLA-C*04:315','HLA-C*04:316','HLA-C*04:317','HLA-C*04:318','HLA-C*04:319','HLA-C*04:32','HLA-C*04:320','HLA-C*04:321','HLA-C*04:322','HLA-C*04:323','HLA-C*04:324','HLA-C*04:325','HLA-C*04:326','HLA-C*04:327','HLA-C*04:328','HLA-C*04:329','HLA-C*04:33','HLA-C*04:330','HLA-C*04:331','HLA-C*04:332','HLA-C*04:333','HLA-C*04:334','HLA-C*04:335','HLA-C*04:336','HLA-C*04:337','HLA-C*04:338','HLA-C*04:339','HLA-C*04:34','HLA-C*04:340','HLA-C*04:341','HLA-C*04:342','HLA-C*04:343','HLA-C*04:344','HLA-C*04:345','HLA-C*04:346','HLA-C*04:347','HLA-C*04:348','HLA-C*04:35','HLA-C*04:351','HLA-C*04:352','HLA-C*04:353','HLA-C*04:354','HLA-C*04:355','HLA-C*04:356','HLA-C*04:357','HLA-C*04:358','HLA-C*04:359','HLA-C*04:36','HLA-C*04:37','HLA-C*04:38','HLA-C*04:39','HLA-C*04:40','HLA-C*04:41','HLA-C*04:42','HLA-C*04:43','HLA-C*04:44','HLA-C*04:45','HLA-C*04:46','HLA-C*04:47','HLA-C*04:48','HLA-C*04:49','HLA-C*04:50','HLA-C*04:51','HLA-C*04:52','HLA-C*04:53','HLA-C*04:54','HLA-C*04:55','HLA-C*04:56','HLA-C*04:57','HLA-C*04:58','HLA-C*04:60','HLA-C*04:61','HLA-C*04:62','HLA-C*04:63','HLA-C*04:64','HLA-C*04:65','HLA-C*04:66','HLA-C*04:67','HLA-C*04:68','HLA-C*04:69','HLA-C*04:70','HLA-C*04:71','HLA-C*04:72','HLA-C*04:73','HLA-C*04:74','HLA-C*04:75','HLA-C*04:76','HLA-C*04:77','HLA-C*04:78','HLA-C*04:79','HLA-C*04:80','HLA-C*04:81','HLA-C*04:82','HLA-C*04:83','HLA-C*04:84','HLA-C*04:85','HLA-C*04:86','HLA-C*04:87','HLA-C*04:89','HLA-C*04:90','HLA-C*04:91','HLA-C*04:92','HLA-C*04:94','HLA-C*04:96','HLA-C*04:97','HLA-C*04:98','HLA-C*04:99','HLA-C*05:01','HLA-C*05:03','HLA-C*05:04','HLA-C*05:05','HLA-C*05:06','HLA-C*05:08','HLA-C*05:09','HLA-C*05:10','HLA-C*05:100','HLA-C*05:101','HLA-C*05:102','HLA-C*05:103','HLA-C*05:104','HLA-C*05:105','HLA-C*05:106','HLA-C*05:107','HLA-C*05:108','HLA-C*05:109','HLA-C*05:11','HLA-C*05:110','HLA-C*05:111','HLA-C*05:112','HLA-C*05:114','HLA-C*05:115','HLA-C*05:116','HLA-C*05:117','HLA-C*05:118','HLA-C*05:119','HLA-C*05:12','HLA-C*05:120','HLA-C*05:121','HLA-C*05:122','HLA-C*05:123','HLA-C*05:124','HLA-C*05:125','HLA-C*05:126','HLA-C*05:127','HLA-C*05:129','HLA-C*05:13','HLA-C*05:130','HLA-C*05:131','HLA-C*05:132','HLA-C*05:133','HLA-C*05:134','HLA-C*05:135','HLA-C*05:136','HLA-C*05:137','HLA-C*05:138','HLA-C*05:139','HLA-C*05:14','HLA-C*05:140','HLA-C*05:141','HLA-C*05:142','HLA-C*05:143','HLA-C*05:144','HLA-C*05:145','HLA-C*05:146','HLA-C*05:147','HLA-C*05:148','HLA-C*05:149','HLA-C*05:15','HLA-C*05:150','HLA-C*05:151','HLA-C*05:152','HLA-C*05:155','HLA-C*05:156','HLA-C*05:157','HLA-C*05:158','HLA-C*05:159','HLA-C*05:16','HLA-C*05:160','HLA-C*05:161','HLA-C*05:162','HLA-C*05:163','HLA-C*05:164','HLA-C*05:165','HLA-C*05:166','HLA-C*05:167','HLA-C*05:168','HLA-C*05:17','HLA-C*05:170','HLA-C*05:171','HLA-C*05:172','HLA-C*05:173','HLA-C*05:174','HLA-C*05:176','HLA-C*05:177','HLA-C*05:178','HLA-C*05:179','HLA-C*05:18','HLA-C*05:181','HLA-C*05:182','HLA-C*05:183','HLA-C*05:184','HLA-C*05:185','HLA-C*05:186','HLA-C*05:187','HLA-C*05:188','HLA-C*05:189','HLA-C*05:19','HLA-C*05:190','HLA-C*05:191','HLA-C*05:192','HLA-C*05:193','HLA-C*05:194','HLA-C*05:195','HLA-C*05:196','HLA-C*05:197','HLA-C*05:198','HLA-C*05:199','HLA-C*05:20','HLA-C*05:200','HLA-C*05:201','HLA-C*05:203','HLA-C*05:21','HLA-C*05:22','HLA-C*05:23','HLA-C*05:24','HLA-C*05:25','HLA-C*05:26','HLA-C*05:27','HLA-C*05:28','HLA-C*05:29','HLA-C*05:30','HLA-C*05:31','HLA-C*05:32','HLA-C*05:33','HLA-C*05:34','HLA-C*05:35','HLA-C*05:36','HLA-C*05:37','HLA-C*05:38','HLA-C*05:39','HLA-C*05:40','HLA-C*05:41','HLA-C*05:42','HLA-C*05:43','HLA-C*05:44','HLA-C*05:45','HLA-C*05:46','HLA-C*05:47','HLA-C*05:49','HLA-C*05:50','HLA-C*05:52','HLA-C*05:53','HLA-C*05:54','HLA-C*05:55','HLA-C*05:56','HLA-C*05:57','HLA-C*05:58','HLA-C*05:59','HLA-C*05:60','HLA-C*05:61','HLA-C*05:62','HLA-C*05:63','HLA-C*05:64','HLA-C*05:65','HLA-C*05:66','HLA-C*05:67','HLA-C*05:68','HLA-C*05:69','HLA-C*05:70','HLA-C*05:71','HLA-C*05:72','HLA-C*05:73','HLA-C*05:74','HLA-C*05:75','HLA-C*05:76','HLA-C*05:77','HLA-C*05:78','HLA-C*05:79','HLA-C*05:80','HLA-C*05:81','HLA-C*05:82','HLA-C*05:83','HLA-C*05:84','HLA-C*05:85','HLA-C*05:86','HLA-C*05:87','HLA-C*05:88','HLA-C*05:89','HLA-C*05:90','HLA-C*05:93','HLA-C*05:94','HLA-C*05:95','HLA-C*05:96','HLA-C*05:97','HLA-C*05:98','HLA-C*06:02','HLA-C*06:03','HLA-C*06:04','HLA-C*06:05','HLA-C*06:06','HLA-C*06:07','HLA-C*06:08','HLA-C*06:09','HLA-C*06:10','HLA-C*06:100','HLA-C*06:101','HLA-C*06:102','HLA-C*06:103','HLA-C*06:104','HLA-C*06:105','HLA-C*06:106','HLA-C*06:107','HLA-C*06:108','HLA-C*06:109','HLA-C*06:11','HLA-C*06:110','HLA-C*06:111','HLA-C*06:112','HLA-C*06:113','HLA-C*06:114','HLA-C*06:115','HLA-C*06:117','HLA-C*06:118','HLA-C*06:119','HLA-C*06:12','HLA-C*06:120','HLA-C*06:121','HLA-C*06:122','HLA-C*06:123','HLA-C*06:124','HLA-C*06:125','HLA-C*06:126','HLA-C*06:127','HLA-C*06:129','HLA-C*06:13','HLA-C*06:130','HLA-C*06:131','HLA-C*06:132','HLA-C*06:133','HLA-C*06:135','HLA-C*06:136','HLA-C*06:137','HLA-C*06:138','HLA-C*06:139','HLA-C*06:14','HLA-C*06:140','HLA-C*06:141','HLA-C*06:142','HLA-C*06:143','HLA-C*06:144','HLA-C*06:145','HLA-C*06:146','HLA-C*06:147','HLA-C*06:148','HLA-C*06:149','HLA-C*06:15','HLA-C*06:150','HLA-C*06:151','HLA-C*06:153','HLA-C*06:154','HLA-C*06:155','HLA-C*06:156','HLA-C*06:157','HLA-C*06:158','HLA-C*06:159','HLA-C*06:160','HLA-C*06:161','HLA-C*06:162','HLA-C*06:163','HLA-C*06:164','HLA-C*06:165','HLA-C*06:166','HLA-C*06:167','HLA-C*06:168','HLA-C*06:169','HLA-C*06:17','HLA-C*06:170','HLA-C*06:172','HLA-C*06:173','HLA-C*06:174','HLA-C*06:176','HLA-C*06:177','HLA-C*06:178','HLA-C*06:179','HLA-C*06:18','HLA-C*06:180','HLA-C*06:181','HLA-C*06:182','HLA-C*06:183','HLA-C*06:184','HLA-C*06:185','HLA-C*06:186','HLA-C*06:187','HLA-C*06:188','HLA-C*06:189','HLA-C*06:19','HLA-C*06:190','HLA-C*06:191','HLA-C*06:192','HLA-C*06:193','HLA-C*06:194','HLA-C*06:195','HLA-C*06:196','HLA-C*06:197','HLA-C*06:198','HLA-C*06:199','HLA-C*06:20','HLA-C*06:201','HLA-C*06:202','HLA-C*06:203','HLA-C*06:204','HLA-C*06:205','HLA-C*06:206','HLA-C*06:207','HLA-C*06:209','HLA-C*06:21','HLA-C*06:210','HLA-C*06:212','HLA-C*06:213','HLA-C*06:214','HLA-C*06:216','HLA-C*06:217','HLA-C*06:218','HLA-C*06:219','HLA-C*06:22','HLA-C*06:221','HLA-C*06:222','HLA-C*06:223','HLA-C*06:224','HLA-C*06:225','HLA-C*06:226','HLA-C*06:227','HLA-C*06:228','HLA-C*06:229','HLA-C*06:23','HLA-C*06:230','HLA-C*06:231','HLA-C*06:232','HLA-C*06:233','HLA-C*06:234','HLA-C*06:235','HLA-C*06:236','HLA-C*06:237','HLA-C*06:238','HLA-C*06:239','HLA-C*06:24','HLA-C*06:240','HLA-C*06:241','HLA-C*06:242','HLA-C*06:243','HLA-C*06:244','HLA-C*06:245','HLA-C*06:246','HLA-C*06:247','HLA-C*06:248','HLA-C*06:249','HLA-C*06:25','HLA-C*06:250','HLA-C*06:251','HLA-C*06:26','HLA-C*06:27','HLA-C*06:28','HLA-C*06:29','HLA-C*06:30','HLA-C*06:31','HLA-C*06:32','HLA-C*06:33','HLA-C*06:34','HLA-C*06:35','HLA-C*06:36','HLA-C*06:37','HLA-C*06:38','HLA-C*06:39','HLA-C*06:40','HLA-C*06:41','HLA-C*06:42','HLA-C*06:43','HLA-C*06:44','HLA-C*06:45','HLA-C*06:47','HLA-C*06:48','HLA-C*06:50','HLA-C*06:51','HLA-C*06:52','HLA-C*06:53','HLA-C*06:54','HLA-C*06:55','HLA-C*06:56','HLA-C*06:57','HLA-C*06:58','HLA-C*06:59','HLA-C*06:60','HLA-C*06:61','HLA-C*06:62','HLA-C*06:63','HLA-C*06:64','HLA-C*06:65','HLA-C*06:66','HLA-C*06:67','HLA-C*06:68','HLA-C*06:69','HLA-C*06:70','HLA-C*06:71','HLA-C*06:72','HLA-C*06:73','HLA-C*06:75','HLA-C*06:76','HLA-C*06:77','HLA-C*06:78','HLA-C*06:80','HLA-C*06:81','HLA-C*06:82','HLA-C*06:83','HLA-C*06:84','HLA-C*06:85','HLA-C*06:86','HLA-C*06:87','HLA-C*06:88','HLA-C*06:89','HLA-C*06:90','HLA-C*06:91','HLA-C*06:92','HLA-C*06:93','HLA-C*06:94','HLA-C*06:95','HLA-C*06:96','HLA-C*06:97','HLA-C*06:98','HLA-C*06:99','HLA-C*07:01','HLA-C*07:02','HLA-C*07:03','HLA-C*07:04','HLA-C*07:05','HLA-C*07:06','HLA-C*07:07','HLA-C*07:08','HLA-C*07:09','HLA-C*07:10','HLA-C*07:100','HLA-C*07:101','HLA-C*07:102','HLA-C*07:103','HLA-C*07:105','HLA-C*07:106','HLA-C*07:107','HLA-C*07:108','HLA-C*07:109','HLA-C*07:11','HLA-C*07:110','HLA-C*07:111','HLA-C*07:112','HLA-C*07:113','HLA-C*07:114','HLA-C*07:115','HLA-C*07:116','HLA-C*07:117','HLA-C*07:118','HLA-C*07:119','HLA-C*07:12','HLA-C*07:120','HLA-C*07:122','HLA-C*07:123','HLA-C*07:124','HLA-C*07:125','HLA-C*07:126','HLA-C*07:127','HLA-C*07:128','HLA-C*07:129','HLA-C*07:13','HLA-C*07:130','HLA-C*07:131','HLA-C*07:132','HLA-C*07:133','HLA-C*07:134','HLA-C*07:135','HLA-C*07:136','HLA-C*07:137','HLA-C*07:138','HLA-C*07:139','HLA-C*07:14','HLA-C*07:140','HLA-C*07:141','HLA-C*07:142','HLA-C*07:143','HLA-C*07:144','HLA-C*07:145','HLA-C*07:146','HLA-C*07:147','HLA-C*07:148','HLA-C*07:149','HLA-C*07:15','HLA-C*07:151','HLA-C*07:153','HLA-C*07:154','HLA-C*07:155','HLA-C*07:156','HLA-C*07:157','HLA-C*07:158','HLA-C*07:159','HLA-C*07:16','HLA-C*07:160','HLA-C*07:161','HLA-C*07:162','HLA-C*07:163','HLA-C*07:165','HLA-C*07:166','HLA-C*07:167','HLA-C*07:168','HLA-C*07:169','HLA-C*07:17','HLA-C*07:170','HLA-C*07:171','HLA-C*07:172','HLA-C*07:173','HLA-C*07:174','HLA-C*07:175','HLA-C*07:176','HLA-C*07:177','HLA-C*07:178','HLA-C*07:179','HLA-C*07:18','HLA-C*07:180','HLA-C*07:181','HLA-C*07:182','HLA-C*07:183','HLA-C*07:184','HLA-C*07:185','HLA-C*07:186','HLA-C*07:187','HLA-C*07:188','HLA-C*07:189','HLA-C*07:19','HLA-C*07:190','HLA-C*07:192','HLA-C*07:193','HLA-C*07:194','HLA-C*07:195','HLA-C*07:196','HLA-C*07:197','HLA-C*07:199','HLA-C*07:20','HLA-C*07:200','HLA-C*07:201','HLA-C*07:202','HLA-C*07:203','HLA-C*07:204','HLA-C*07:205','HLA-C*07:206','HLA-C*07:207','HLA-C*07:208','HLA-C*07:209','HLA-C*07:21','HLA-C*07:210','HLA-C*07:211','HLA-C*07:212','HLA-C*07:213','HLA-C*07:214','HLA-C*07:215','HLA-C*07:216','HLA-C*07:217','HLA-C*07:218','HLA-C*07:219','HLA-C*07:22','HLA-C*07:220','HLA-C*07:221','HLA-C*07:222','HLA-C*07:223','HLA-C*07:224','HLA-C*07:225','HLA-C*07:226','HLA-C*07:228','HLA-C*07:229','HLA-C*07:23','HLA-C*07:230','HLA-C*07:231','HLA-C*07:232','HLA-C*07:233','HLA-C*07:234','HLA-C*07:236','HLA-C*07:237','HLA-C*07:238','HLA-C*07:239','HLA-C*07:24','HLA-C*07:240','HLA-C*07:241','HLA-C*07:242','HLA-C*07:243','HLA-C*07:244','HLA-C*07:245','HLA-C*07:246','HLA-C*07:247','HLA-C*07:248','HLA-C*07:249','HLA-C*07:25','HLA-C*07:250','HLA-C*07:251','HLA-C*07:252','HLA-C*07:253','HLA-C*07:254','HLA-C*07:255','HLA-C*07:256','HLA-C*07:257','HLA-C*07:258','HLA-C*07:259','HLA-C*07:26','HLA-C*07:260','HLA-C*07:261','HLA-C*07:262','HLA-C*07:263','HLA-C*07:265','HLA-C*07:266','HLA-C*07:267','HLA-C*07:268','HLA-C*07:269','HLA-C*07:27','HLA-C*07:270','HLA-C*07:271','HLA-C*07:272','HLA-C*07:273','HLA-C*07:274','HLA-C*07:275','HLA-C*07:276','HLA-C*07:277','HLA-C*07:278','HLA-C*07:279','HLA-C*07:28','HLA-C*07:280','HLA-C*07:281','HLA-C*07:282','HLA-C*07:283','HLA-C*07:284','HLA-C*07:285','HLA-C*07:286','HLA-C*07:287','HLA-C*07:288','HLA-C*07:289','HLA-C*07:29','HLA-C*07:290','HLA-C*07:291','HLA-C*07:292','HLA-C*07:293','HLA-C*07:294','HLA-C*07:296','HLA-C*07:297','HLA-C*07:298','HLA-C*07:299','HLA-C*07:30','HLA-C*07:300','HLA-C*07:301','HLA-C*07:302','HLA-C*07:303','HLA-C*07:304','HLA-C*07:305','HLA-C*07:306','HLA-C*07:307','HLA-C*07:308','HLA-C*07:309','HLA-C*07:31','HLA-C*07:310','HLA-C*07:311','HLA-C*07:312','HLA-C*07:313','HLA-C*07:314','HLA-C*07:315','HLA-C*07:316','HLA-C*07:317','HLA-C*07:318','HLA-C*07:319','HLA-C*07:320','HLA-C*07:321','HLA-C*07:322','HLA-C*07:323','HLA-C*07:324','HLA-C*07:325','HLA-C*07:326','HLA-C*07:327','HLA-C*07:328','HLA-C*07:330','HLA-C*07:331','HLA-C*07:332','HLA-C*07:333','HLA-C*07:334','HLA-C*07:335','HLA-C*07:336','HLA-C*07:337','HLA-C*07:338','HLA-C*07:339','HLA-C*07:340','HLA-C*07:341','HLA-C*07:342','HLA-C*07:343','HLA-C*07:344','HLA-C*07:345','HLA-C*07:346','HLA-C*07:348','HLA-C*07:349','HLA-C*07:35','HLA-C*07:351','HLA-C*07:352','HLA-C*07:353','HLA-C*07:354','HLA-C*07:355','HLA-C*07:356','HLA-C*07:357','HLA-C*07:358','HLA-C*07:359','HLA-C*07:36','HLA-C*07:360','HLA-C*07:361','HLA-C*07:362','HLA-C*07:363','HLA-C*07:364','HLA-C*07:365','HLA-C*07:366','HLA-C*07:367','HLA-C*07:368','HLA-C*07:369','HLA-C*07:37','HLA-C*07:370','HLA-C*07:371','HLA-C*07:372','HLA-C*07:373','HLA-C*07:374','HLA-C*07:375','HLA-C*07:376','HLA-C*07:377','HLA-C*07:378','HLA-C*07:379','HLA-C*07:38','HLA-C*07:380','HLA-C*07:381','HLA-C*07:382','HLA-C*07:383','HLA-C*07:384','HLA-C*07:385','HLA-C*07:386','HLA-C*07:387','HLA-C*07:388','HLA-C*07:389','HLA-C*07:39','HLA-C*07:390','HLA-C*07:391','HLA-C*07:392','HLA-C*07:394','HLA-C*07:395','HLA-C*07:396','HLA-C*07:397','HLA-C*07:398','HLA-C*07:399','HLA-C*07:40','HLA-C*07:400','HLA-C*07:401','HLA-C*07:402','HLA-C*07:403','HLA-C*07:404','HLA-C*07:405','HLA-C*07:406','HLA-C*07:407','HLA-C*07:408','HLA-C*07:409','HLA-C*07:41','HLA-C*07:410','HLA-C*07:411','HLA-C*07:412','HLA-C*07:413','HLA-C*07:414','HLA-C*07:415','HLA-C*07:416','HLA-C*07:417','HLA-C*07:418','HLA-C*07:419','HLA-C*07:42','HLA-C*07:420','HLA-C*07:421','HLA-C*07:422','HLA-C*07:423','HLA-C*07:424','HLA-C*07:425','HLA-C*07:426','HLA-C*07:427','HLA-C*07:428','HLA-C*07:429','HLA-C*07:43','HLA-C*07:430','HLA-C*07:431','HLA-C*07:432','HLA-C*07:433','HLA-C*07:434','HLA-C*07:435','HLA-C*07:436','HLA-C*07:438','HLA-C*07:439','HLA-C*07:44','HLA-C*07:440','HLA-C*07:441','HLA-C*07:442','HLA-C*07:443','HLA-C*07:444','HLA-C*07:445','HLA-C*07:446','HLA-C*07:447','HLA-C*07:448','HLA-C*07:449','HLA-C*07:45','HLA-C*07:450','HLA-C*07:453','HLA-C*07:454','HLA-C*07:455','HLA-C*07:456','HLA-C*07:457','HLA-C*07:458','HLA-C*07:459','HLA-C*07:46','HLA-C*07:460','HLA-C*07:461','HLA-C*07:462','HLA-C*07:463','HLA-C*07:464','HLA-C*07:465','HLA-C*07:466','HLA-C*07:467','HLA-C*07:468','HLA-C*07:469','HLA-C*07:47','HLA-C*07:470','HLA-C*07:471','HLA-C*07:472','HLA-C*07:473','HLA-C*07:474','HLA-C*07:475','HLA-C*07:477','HLA-C*07:478','HLA-C*07:479','HLA-C*07:48','HLA-C*07:480','HLA-C*07:481','HLA-C*07:482','HLA-C*07:485','HLA-C*07:486','HLA-C*07:487','HLA-C*07:488','HLA-C*07:489','HLA-C*07:49','HLA-C*07:490','HLA-C*07:492','HLA-C*07:493','HLA-C*07:495','HLA-C*07:496','HLA-C*07:497','HLA-C*07:498','HLA-C*07:499','HLA-C*07:50','HLA-C*07:500','HLA-C*07:501','HLA-C*07:502','HLA-C*07:503','HLA-C*07:504','HLA-C*07:505','HLA-C*07:506','HLA-C*07:508','HLA-C*07:509','HLA-C*07:51','HLA-C*07:510','HLA-C*07:511','HLA-C*07:512','HLA-C*07:514','HLA-C*07:515','HLA-C*07:516','HLA-C*07:517','HLA-C*07:518','HLA-C*07:519','HLA-C*07:52','HLA-C*07:520','HLA-C*07:521','HLA-C*07:522','HLA-C*07:523','HLA-C*07:524','HLA-C*07:525','HLA-C*07:526','HLA-C*07:527','HLA-C*07:528','HLA-C*07:529','HLA-C*07:53','HLA-C*07:530','HLA-C*07:531','HLA-C*07:532','HLA-C*07:533','HLA-C*07:534','HLA-C*07:535','HLA-C*07:536','HLA-C*07:537','HLA-C*07:538','HLA-C*07:539','HLA-C*07:54','HLA-C*07:540','HLA-C*07:541','HLA-C*07:542','HLA-C*07:543','HLA-C*07:544','HLA-C*07:545','HLA-C*07:546','HLA-C*07:547','HLA-C*07:548','HLA-C*07:549','HLA-C*07:550','HLA-C*07:552','HLA-C*07:553','HLA-C*07:554','HLA-C*07:555','HLA-C*07:556','HLA-C*07:557','HLA-C*07:558','HLA-C*07:559','HLA-C*07:56','HLA-C*07:560','HLA-C*07:561','HLA-C*07:562','HLA-C*07:563','HLA-C*07:564','HLA-C*07:565','HLA-C*07:566','HLA-C*07:567','HLA-C*07:568','HLA-C*07:569','HLA-C*07:57','HLA-C*07:570','HLA-C*07:571','HLA-C*07:572','HLA-C*07:573','HLA-C*07:574','HLA-C*07:575','HLA-C*07:576','HLA-C*07:577','HLA-C*07:578','HLA-C*07:579','HLA-C*07:58','HLA-C*07:580','HLA-C*07:581','HLA-C*07:583','HLA-C*07:584','HLA-C*07:585','HLA-C*07:586','HLA-C*07:587','HLA-C*07:588','HLA-C*07:589','HLA-C*07:59','HLA-C*07:590','HLA-C*07:591','HLA-C*07:592','HLA-C*07:594','HLA-C*07:595','HLA-C*07:596','HLA-C*07:597','HLA-C*07:598','HLA-C*07:599','HLA-C*07:60','HLA-C*07:601','HLA-C*07:602','HLA-C*07:604','HLA-C*07:605','HLA-C*07:606','HLA-C*07:607','HLA-C*07:608','HLA-C*07:609','HLA-C*07:610','HLA-C*07:611','HLA-C*07:612','HLA-C*07:613','HLA-C*07:614','HLA-C*07:615','HLA-C*07:616','HLA-C*07:617','HLA-C*07:618','HLA-C*07:619','HLA-C*07:62','HLA-C*07:620','HLA-C*07:621','HLA-C*07:622','HLA-C*07:623','HLA-C*07:624','HLA-C*07:625','HLA-C*07:626','HLA-C*07:627','HLA-C*07:628','HLA-C*07:629','HLA-C*07:63','HLA-C*07:630','HLA-C*07:631','HLA-C*07:634','HLA-C*07:635','HLA-C*07:636','HLA-C*07:637','HLA-C*07:638','HLA-C*07:639','HLA-C*07:64','HLA-C*07:640','HLA-C*07:641','HLA-C*07:642','HLA-C*07:643','HLA-C*07:644','HLA-C*07:645','HLA-C*07:646','HLA-C*07:647','HLA-C*07:648','HLA-C*07:649','HLA-C*07:65','HLA-C*07:650','HLA-C*07:651','HLA-C*07:652','HLA-C*07:653','HLA-C*07:654','HLA-C*07:655','HLA-C*07:656','HLA-C*07:657','HLA-C*07:658','HLA-C*07:659','HLA-C*07:66','HLA-C*07:660','HLA-C*07:661','HLA-C*07:662','HLA-C*07:664','HLA-C*07:665','HLA-C*07:666','HLA-C*07:667','HLA-C*07:668','HLA-C*07:669','HLA-C*07:67','HLA-C*07:670','HLA-C*07:671','HLA-C*07:673','HLA-C*07:674','HLA-C*07:676','HLA-C*07:677','HLA-C*07:678','HLA-C*07:679','HLA-C*07:68','HLA-C*07:680','HLA-C*07:681','HLA-C*07:682','HLA-C*07:683','HLA-C*07:684','HLA-C*07:685','HLA-C*07:687','HLA-C*07:688','HLA-C*07:689','HLA-C*07:69','HLA-C*07:691','HLA-C*07:692','HLA-C*07:693','HLA-C*07:694','HLA-C*07:695','HLA-C*07:696','HLA-C*07:698','HLA-C*07:699','HLA-C*07:70','HLA-C*07:700','HLA-C*07:701','HLA-C*07:703','HLA-C*07:704','HLA-C*07:705','HLA-C*07:706','HLA-C*07:707','HLA-C*07:708','HLA-C*07:709','HLA-C*07:71','HLA-C*07:710','HLA-C*07:711','HLA-C*07:712','HLA-C*07:713','HLA-C*07:714','HLA-C*07:715','HLA-C*07:716','HLA-C*07:717','HLA-C*07:718','HLA-C*07:719','HLA-C*07:72','HLA-C*07:720','HLA-C*07:721','HLA-C*07:722','HLA-C*07:723','HLA-C*07:724','HLA-C*07:73','HLA-C*07:74','HLA-C*07:75','HLA-C*07:76','HLA-C*07:77','HLA-C*07:78','HLA-C*07:79','HLA-C*07:80','HLA-C*07:81','HLA-C*07:82','HLA-C*07:83','HLA-C*07:84','HLA-C*07:85','HLA-C*07:86','HLA-C*07:87','HLA-C*07:88','HLA-C*07:89','HLA-C*07:90','HLA-C*07:91','HLA-C*07:92','HLA-C*07:93','HLA-C*07:94','HLA-C*07:95','HLA-C*07:96','HLA-C*07:97','HLA-C*07:99','HLA-C*08:01','HLA-C*08:02','HLA-C*08:03','HLA-C*08:04','HLA-C*08:05','HLA-C*08:06','HLA-C*08:07','HLA-C*08:08','HLA-C*08:09','HLA-C*08:10','HLA-C*08:100','HLA-C*08:101','HLA-C*08:102','HLA-C*08:103','HLA-C*08:104','HLA-C*08:105','HLA-C*08:106','HLA-C*08:107','HLA-C*08:108','HLA-C*08:109','HLA-C*08:11','HLA-C*08:110','HLA-C*08:111','HLA-C*08:112','HLA-C*08:113','HLA-C*08:114','HLA-C*08:115','HLA-C*08:116','HLA-C*08:117','HLA-C*08:118','HLA-C*08:119','HLA-C*08:12','HLA-C*08:120','HLA-C*08:122','HLA-C*08:123','HLA-C*08:124','HLA-C*08:125','HLA-C*08:126','HLA-C*08:128','HLA-C*08:13','HLA-C*08:131','HLA-C*08:132','HLA-C*08:133','HLA-C*08:134','HLA-C*08:135','HLA-C*08:136','HLA-C*08:137','HLA-C*08:138','HLA-C*08:139','HLA-C*08:14','HLA-C*08:140','HLA-C*08:142','HLA-C*08:143','HLA-C*08:144','HLA-C*08:145','HLA-C*08:146','HLA-C*08:147','HLA-C*08:148','HLA-C*08:149','HLA-C*08:15','HLA-C*08:150','HLA-C*08:151','HLA-C*08:152','HLA-C*08:153','HLA-C*08:154','HLA-C*08:155','HLA-C*08:156','HLA-C*08:157','HLA-C*08:158','HLA-C*08:159','HLA-C*08:16','HLA-C*08:160','HLA-C*08:162','HLA-C*08:163','HLA-C*08:164','HLA-C*08:165','HLA-C*08:166','HLA-C*08:167','HLA-C*08:168','HLA-C*08:169','HLA-C*08:17','HLA-C*08:170','HLA-C*08:171','HLA-C*08:172','HLA-C*08:174','HLA-C*08:175','HLA-C*08:176','HLA-C*08:177','HLA-C*08:178','HLA-C*08:18','HLA-C*08:19','HLA-C*08:20','HLA-C*08:21','HLA-C*08:22','HLA-C*08:23','HLA-C*08:24','HLA-C*08:25','HLA-C*08:27','HLA-C*08:28','HLA-C*08:29','HLA-C*08:30','HLA-C*08:31','HLA-C*08:32','HLA-C*08:33','HLA-C*08:34','HLA-C*08:35','HLA-C*08:37','HLA-C*08:38','HLA-C*08:39','HLA-C*08:40','HLA-C*08:41','HLA-C*08:42','HLA-C*08:43','HLA-C*08:44','HLA-C*08:45','HLA-C*08:46','HLA-C*08:47','HLA-C*08:48','HLA-C*08:49','HLA-C*08:50','HLA-C*08:51','HLA-C*08:53','HLA-C*08:54','HLA-C*08:56','HLA-C*08:57','HLA-C*08:58','HLA-C*08:59','HLA-C*08:60','HLA-C*08:61','HLA-C*08:62','HLA-C*08:63','HLA-C*08:65','HLA-C*08:66','HLA-C*08:67','HLA-C*08:68','HLA-C*08:69','HLA-C*08:71','HLA-C*08:72','HLA-C*08:73','HLA-C*08:74','HLA-C*08:75','HLA-C*08:76','HLA-C*08:77','HLA-C*08:78','HLA-C*08:79','HLA-C*08:80','HLA-C*08:81','HLA-C*08:82','HLA-C*08:83','HLA-C*08:84','HLA-C*08:85','HLA-C*08:86','HLA-C*08:87','HLA-C*08:90','HLA-C*08:91','HLA-C*08:92','HLA-C*08:93','HLA-C*08:94','HLA-C*08:95','HLA-C*08:96','HLA-C*08:97','HLA-C*08:98','HLA-C*08:99','HLA-C*12:02','HLA-C*12:03','HLA-C*12:04','HLA-C*12:05','HLA-C*12:06','HLA-C*12:07','HLA-C*12:08','HLA-C*12:09','HLA-C*12:10','HLA-C*12:100','HLA-C*12:101','HLA-C*12:102','HLA-C*12:103','HLA-C*12:106','HLA-C*12:107','HLA-C*12:108','HLA-C*12:109','HLA-C*12:11','HLA-C*12:110','HLA-C*12:111','HLA-C*12:112','HLA-C*12:113','HLA-C*12:114','HLA-C*12:115','HLA-C*12:116','HLA-C*12:117','HLA-C*12:118','HLA-C*12:119','HLA-C*12:12','HLA-C*12:120','HLA-C*12:121','HLA-C*12:122','HLA-C*12:123','HLA-C*12:124','HLA-C*12:125','HLA-C*12:126','HLA-C*12:127','HLA-C*12:128','HLA-C*12:129','HLA-C*12:13','HLA-C*12:130','HLA-C*12:131','HLA-C*12:132','HLA-C*12:133','HLA-C*12:134','HLA-C*12:135','HLA-C*12:136','HLA-C*12:137','HLA-C*12:138','HLA-C*12:139','HLA-C*12:14','HLA-C*12:140','HLA-C*12:141','HLA-C*12:142','HLA-C*12:143','HLA-C*12:144','HLA-C*12:145','HLA-C*12:146','HLA-C*12:147','HLA-C*12:149','HLA-C*12:15','HLA-C*12:150','HLA-C*12:151','HLA-C*12:152','HLA-C*12:153','HLA-C*12:154','HLA-C*12:156','HLA-C*12:157','HLA-C*12:158','HLA-C*12:159','HLA-C*12:16','HLA-C*12:160','HLA-C*12:161','HLA-C*12:162','HLA-C*12:163','HLA-C*12:164','HLA-C*12:165','HLA-C*12:166','HLA-C*12:167','HLA-C*12:168','HLA-C*12:169','HLA-C*12:17','HLA-C*12:170','HLA-C*12:171','HLA-C*12:172','HLA-C*12:173','HLA-C*12:174','HLA-C*12:175','HLA-C*12:176','HLA-C*12:177','HLA-C*12:178','HLA-C*12:179','HLA-C*12:18','HLA-C*12:180','HLA-C*12:181','HLA-C*12:182','HLA-C*12:183','HLA-C*12:184','HLA-C*12:185','HLA-C*12:186','HLA-C*12:187','HLA-C*12:188','HLA-C*12:189','HLA-C*12:19','HLA-C*12:190','HLA-C*12:191','HLA-C*12:192','HLA-C*12:193','HLA-C*12:194','HLA-C*12:195','HLA-C*12:196','HLA-C*12:197','HLA-C*12:198','HLA-C*12:199','HLA-C*12:20','HLA-C*12:200','HLA-C*12:201','HLA-C*12:202','HLA-C*12:203','HLA-C*12:204','HLA-C*12:205','HLA-C*12:206','HLA-C*12:207','HLA-C*12:208','HLA-C*12:209','HLA-C*12:21','HLA-C*12:210','HLA-C*12:211','HLA-C*12:212','HLA-C*12:213','HLA-C*12:214','HLA-C*12:215','HLA-C*12:216','HLA-C*12:217','HLA-C*12:218','HLA-C*12:22','HLA-C*12:220','HLA-C*12:221','HLA-C*12:222','HLA-C*12:223','HLA-C*12:224','HLA-C*12:225','HLA-C*12:226','HLA-C*12:227','HLA-C*12:228','HLA-C*12:229','HLA-C*12:23','HLA-C*12:230','HLA-C*12:231','HLA-C*12:233','HLA-C*12:234','HLA-C*12:235','HLA-C*12:237','HLA-C*12:238','HLA-C*12:239','HLA-C*12:24','HLA-C*12:240','HLA-C*12:241','HLA-C*12:242','HLA-C*12:243','HLA-C*12:244','HLA-C*12:245','HLA-C*12:246','HLA-C*12:247','HLA-C*12:248','HLA-C*12:249','HLA-C*12:25','HLA-C*12:250','HLA-C*12:251','HLA-C*12:252','HLA-C*12:253','HLA-C*12:254','HLA-C*12:255','HLA-C*12:256','HLA-C*12:257','HLA-C*12:258','HLA-C*12:259','HLA-C*12:26','HLA-C*12:260','HLA-C*12:261','HLA-C*12:262','HLA-C*12:263','HLA-C*12:264','HLA-C*12:265','HLA-C*12:266','HLA-C*12:267','HLA-C*12:27','HLA-C*12:28','HLA-C*12:29','HLA-C*12:30','HLA-C*12:31','HLA-C*12:32','HLA-C*12:33','HLA-C*12:34','HLA-C*12:35','HLA-C*12:36','HLA-C*12:37','HLA-C*12:38','HLA-C*12:40','HLA-C*12:41','HLA-C*12:43','HLA-C*12:44','HLA-C*12:45','HLA-C*12:47','HLA-C*12:48','HLA-C*12:49','HLA-C*12:50','HLA-C*12:51','HLA-C*12:52','HLA-C*12:53','HLA-C*12:54','HLA-C*12:55','HLA-C*12:56','HLA-C*12:57','HLA-C*12:58','HLA-C*12:59','HLA-C*12:60','HLA-C*12:61','HLA-C*12:62','HLA-C*12:63','HLA-C*12:64','HLA-C*12:65','HLA-C*12:66','HLA-C*12:67','HLA-C*12:68','HLA-C*12:69','HLA-C*12:70','HLA-C*12:71','HLA-C*12:72','HLA-C*12:73','HLA-C*12:74','HLA-C*12:75','HLA-C*12:76','HLA-C*12:77','HLA-C*12:78','HLA-C*12:79','HLA-C*12:81','HLA-C*12:82','HLA-C*12:83','HLA-C*12:85','HLA-C*12:86','HLA-C*12:87','HLA-C*12:88','HLA-C*12:89','HLA-C*12:90','HLA-C*12:91','HLA-C*12:92','HLA-C*12:93','HLA-C*12:94','HLA-C*12:95','HLA-C*12:96','HLA-C*12:97','HLA-C*12:98','HLA-C*12:99','HLA-C*14:02','HLA-C*14:03','HLA-C*14:04','HLA-C*14:05','HLA-C*14:06','HLA-C*14:08','HLA-C*14:09','HLA-C*14:10','HLA-C*14:100','HLA-C*14:101','HLA-C*14:102','HLA-C*14:103','HLA-C*14:104','HLA-C*14:11','HLA-C*14:12','HLA-C*14:13','HLA-C*14:14','HLA-C*14:15','HLA-C*14:16','HLA-C*14:17','HLA-C*14:18','HLA-C*14:19','HLA-C*14:20','HLA-C*14:22','HLA-C*14:23','HLA-C*14:24','HLA-C*14:25','HLA-C*14:26','HLA-C*14:27','HLA-C*14:28','HLA-C*14:29','HLA-C*14:30','HLA-C*14:31','HLA-C*14:32','HLA-C*14:33','HLA-C*14:34','HLA-C*14:36','HLA-C*14:37','HLA-C*14:38','HLA-C*14:39','HLA-C*14:40','HLA-C*14:41','HLA-C*14:42','HLA-C*14:43','HLA-C*14:44','HLA-C*14:45','HLA-C*14:46','HLA-C*14:48','HLA-C*14:49','HLA-C*14:50','HLA-C*14:51','HLA-C*14:52','HLA-C*14:53','HLA-C*14:54','HLA-C*14:55','HLA-C*14:56','HLA-C*14:57','HLA-C*14:58','HLA-C*14:59','HLA-C*14:60','HLA-C*14:61','HLA-C*14:62','HLA-C*14:63','HLA-C*14:64','HLA-C*14:65','HLA-C*14:66','HLA-C*14:67','HLA-C*14:68','HLA-C*14:69','HLA-C*14:70','HLA-C*14:71','HLA-C*14:72','HLA-C*14:73','HLA-C*14:74','HLA-C*14:75','HLA-C*14:76','HLA-C*14:77','HLA-C*14:78','HLA-C*14:79','HLA-C*14:80','HLA-C*14:81','HLA-C*14:82','HLA-C*14:83','HLA-C*14:84','HLA-C*14:85','HLA-C*14:86','HLA-C*14:87','HLA-C*14:88','HLA-C*14:89','HLA-C*14:90','HLA-C*14:91','HLA-C*14:92','HLA-C*14:94','HLA-C*14:95','HLA-C*14:96','HLA-C*14:98','HLA-C*15:02','HLA-C*15:03','HLA-C*15:04','HLA-C*15:05','HLA-C*15:06','HLA-C*15:07','HLA-C*15:08','HLA-C*15:09','HLA-C*15:10','HLA-C*15:100','HLA-C*15:101','HLA-C*15:102','HLA-C*15:103','HLA-C*15:104','HLA-C*15:106','HLA-C*15:107','HLA-C*15:108','HLA-C*15:109','HLA-C*15:11','HLA-C*15:110','HLA-C*15:111','HLA-C*15:112','HLA-C*15:113','HLA-C*15:114','HLA-C*15:116','HLA-C*15:117','HLA-C*15:118','HLA-C*15:119','HLA-C*15:12','HLA-C*15:120','HLA-C*15:121','HLA-C*15:123','HLA-C*15:124','HLA-C*15:125','HLA-C*15:126','HLA-C*15:127','HLA-C*15:128','HLA-C*15:129','HLA-C*15:13','HLA-C*15:130','HLA-C*15:131','HLA-C*15:132','HLA-C*15:133','HLA-C*15:134','HLA-C*15:135','HLA-C*15:136','HLA-C*15:137','HLA-C*15:138','HLA-C*15:139','HLA-C*15:140','HLA-C*15:141','HLA-C*15:142','HLA-C*15:143','HLA-C*15:144','HLA-C*15:146','HLA-C*15:147','HLA-C*15:148','HLA-C*15:149','HLA-C*15:15','HLA-C*15:150','HLA-C*15:151','HLA-C*15:152','HLA-C*15:153','HLA-C*15:154','HLA-C*15:155','HLA-C*15:157','HLA-C*15:158','HLA-C*15:159','HLA-C*15:16','HLA-C*15:161','HLA-C*15:162','HLA-C*15:163','HLA-C*15:165','HLA-C*15:166','HLA-C*15:167','HLA-C*15:168','HLA-C*15:169','HLA-C*15:17','HLA-C*15:170','HLA-C*15:171','HLA-C*15:172','HLA-C*15:173','HLA-C*15:174','HLA-C*15:175','HLA-C*15:176','HLA-C*15:178','HLA-C*15:179','HLA-C*15:18','HLA-C*15:180','HLA-C*15:181','HLA-C*15:182','HLA-C*15:183','HLA-C*15:19','HLA-C*15:20','HLA-C*15:21','HLA-C*15:22','HLA-C*15:23','HLA-C*15:24','HLA-C*15:25','HLA-C*15:26','HLA-C*15:27','HLA-C*15:28','HLA-C*15:29','HLA-C*15:30','HLA-C*15:31','HLA-C*15:33','HLA-C*15:34','HLA-C*15:35','HLA-C*15:36','HLA-C*15:37','HLA-C*15:38','HLA-C*15:39','HLA-C*15:40','HLA-C*15:41','HLA-C*15:42','HLA-C*15:43','HLA-C*15:44','HLA-C*15:45','HLA-C*15:46','HLA-C*15:47','HLA-C*15:48','HLA-C*15:49','HLA-C*15:50','HLA-C*15:51','HLA-C*15:52','HLA-C*15:53','HLA-C*15:54','HLA-C*15:55','HLA-C*15:56','HLA-C*15:57','HLA-C*15:58','HLA-C*15:59','HLA-C*15:60','HLA-C*15:61','HLA-C*15:62','HLA-C*15:63','HLA-C*15:64','HLA-C*15:65','HLA-C*15:66','HLA-C*15:67','HLA-C*15:68','HLA-C*15:69','HLA-C*15:70','HLA-C*15:71','HLA-C*15:72','HLA-C*15:73','HLA-C*15:74','HLA-C*15:75','HLA-C*15:76','HLA-C*15:77','HLA-C*15:78','HLA-C*15:79','HLA-C*15:80','HLA-C*15:81','HLA-C*15:82','HLA-C*15:83','HLA-C*15:85','HLA-C*15:86','HLA-C*15:87','HLA-C*15:88','HLA-C*15:89','HLA-C*15:90','HLA-C*15:91','HLA-C*15:93','HLA-C*15:94','HLA-C*15:97','HLA-C*15:98','HLA-C*15:99','HLA-C*16:01','HLA-C*16:02','HLA-C*16:04','HLA-C*16:06','HLA-C*16:07','HLA-C*16:08','HLA-C*16:09','HLA-C*16:10','HLA-C*16:100','HLA-C*16:101','HLA-C*16:102','HLA-C*16:103','HLA-C*16:104','HLA-C*16:105','HLA-C*16:106','HLA-C*16:107','HLA-C*16:108','HLA-C*16:109','HLA-C*16:11','HLA-C*16:110','HLA-C*16:111','HLA-C*16:112','HLA-C*16:113','HLA-C*16:114','HLA-C*16:115','HLA-C*16:116','HLA-C*16:117','HLA-C*16:118','HLA-C*16:119','HLA-C*16:12','HLA-C*16:120','HLA-C*16:121','HLA-C*16:122','HLA-C*16:124','HLA-C*16:125','HLA-C*16:126','HLA-C*16:127','HLA-C*16:128','HLA-C*16:129','HLA-C*16:13','HLA-C*16:130','HLA-C*16:131','HLA-C*16:133','HLA-C*16:134','HLA-C*16:135','HLA-C*16:136','HLA-C*16:137','HLA-C*16:138','HLA-C*16:139','HLA-C*16:14','HLA-C*16:140','HLA-C*16:141','HLA-C*16:142','HLA-C*16:143','HLA-C*16:144','HLA-C*16:145','HLA-C*16:146','HLA-C*16:15','HLA-C*16:17','HLA-C*16:18','HLA-C*16:19','HLA-C*16:20','HLA-C*16:21','HLA-C*16:22','HLA-C*16:23','HLA-C*16:24','HLA-C*16:25','HLA-C*16:26','HLA-C*16:27','HLA-C*16:28','HLA-C*16:29','HLA-C*16:31','HLA-C*16:32','HLA-C*16:33','HLA-C*16:34','HLA-C*16:35','HLA-C*16:36','HLA-C*16:37','HLA-C*16:38','HLA-C*16:39','HLA-C*16:40','HLA-C*16:41','HLA-C*16:42','HLA-C*16:43','HLA-C*16:44','HLA-C*16:45','HLA-C*16:46','HLA-C*16:47','HLA-C*16:48','HLA-C*16:49','HLA-C*16:50','HLA-C*16:51','HLA-C*16:52','HLA-C*16:53','HLA-C*16:54','HLA-C*16:55','HLA-C*16:56','HLA-C*16:57','HLA-C*16:58','HLA-C*16:59','HLA-C*16:60','HLA-C*16:61','HLA-C*16:62','HLA-C*16:63','HLA-C*16:64','HLA-C*16:65','HLA-C*16:66','HLA-C*16:67','HLA-C*16:68','HLA-C*16:69','HLA-C*16:70','HLA-C*16:71','HLA-C*16:72','HLA-C*16:73','HLA-C*16:74','HLA-C*16:75','HLA-C*16:76','HLA-C*16:78','HLA-C*16:79','HLA-C*16:80','HLA-C*16:81','HLA-C*16:82','HLA-C*16:83','HLA-C*16:84','HLA-C*16:85','HLA-C*16:86','HLA-C*16:87','HLA-C*16:88','HLA-C*16:90','HLA-C*16:91','HLA-C*16:92','HLA-C*16:93','HLA-C*16:94','HLA-C*16:95','HLA-C*16:96','HLA-C*16:97','HLA-C*16:98','HLA-C*16:99','HLA-C*17:01','HLA-C*17:02','HLA-C*17:03','HLA-C*17:04','HLA-C*17:05','HLA-C*17:06','HLA-C*17:07','HLA-C*17:08','HLA-C*17:09','HLA-C*17:10','HLA-C*17:11','HLA-C*17:12','HLA-C*17:13','HLA-C*17:14','HLA-C*17:15','HLA-C*17:16','HLA-C*17:17','HLA-C*17:18','HLA-C*17:19','HLA-C*17:20','HLA-C*17:21','HLA-C*17:22','HLA-C*17:23','HLA-C*17:24','HLA-C*17:25','HLA-C*17:26','HLA-C*17:28','HLA-C*17:29','HLA-C*17:30','HLA-C*17:31','HLA-C*17:32','HLA-C*17:33','HLA-C*17:34','HLA-C*17:35','HLA-C*17:36','HLA-C*17:37','HLA-C*17:38','HLA-C*17:39','HLA-C*17:40','HLA-C*17:41','HLA-C*18:01','HLA-C*18:02','HLA-C*18:03','HLA-C*18:04','HLA-C*18:05','HLA-C*18:06','HLA-C*18:08','HLA-C*18:09','HLA-C*18:10','HLA-C*18:11','HLA-C*18:12','HLA-E*01:01','HLA-E*01:03','HLA-G*01:01','HLA-G*01:02','HLA-G*01:03','HLA-G*01:04','HLA-G*01:06','HLA-G*01:07','HLA-G*01:08','HLA-G*01:09'])

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter = '\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in next(f) if x != ""]
        next(f, None) # Avoid header column, only access raw and rank scores
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCPAN_4_1]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCPAN_4_1 + i * Offset.NETMHCPAN_4_1])
                ranks[a][pep_seq] = float(row[RankIndex.NETMHCPAN_4_1 + i * Offset.NETMHCPAN_4_1])
        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
        return result


class NetMHCstabpan_1_0(AExternalEpitopePrediction):
    """
    Implements a wrapper to NetMHCstabpan 1.0

    .. note:

    Pan-specific prediction of peptide-MHC-I complex stability; a correlate of T cell immunogenicity
    M Rasmussen, E Fenoy, M Nielsen, Buus S, Accepted JI June, 2016
    """
    __name = "netMHCstabpan"
    __length = frozenset([8, 9, 10, 11])
    __version = "1.0"
    __command = "netMHCstabpan -p {peptides} -a {alleles} {options} -xls -xlsfile {out}"
    __alleles = frozenset(['HLA-A*01:01', 'HLA-A*01:02', 'HLA-A*01:03', 'HLA-A*01:06', 'HLA-A*01:07', 'HLA-A*01:08', 'HLA-A*01:09', 'HLA-A*01:10', 'HLA-A*01:12',
                            'HLA-A*01:13', 'HLA-A*01:14', 'HLA-A*01:17', 'HLA-A*01:19', 'HLA-A*01:20', 'HLA-A*01:21', 'HLA-A*01:23', 'HLA-A*01:24',
                            'HLA-A*01:25', 'HLA-A*01:26', 'HLA-A*01:28', 'HLA-A*01:29', 'HLA-A*01:30', 'HLA-A*01:32', 'HLA-A*01:33', 'HLA-A*01:35',
                            'HLA-A*01:36', 'HLA-A*01:37', 'HLA-A*01:38', 'HLA-A*01:39', 'HLA-A*01:40', 'HLA-A*01:41', 'HLA-A*01:42', 'HLA-A*01:43',
                            'HLA-A*01:44', 'HLA-A*01:45', 'HLA-A*01:46', 'HLA-A*01:47', 'HLA-A*01:48', 'HLA-A*01:49', 'HLA-A*01:50', 'HLA-A*01:51',
                            'HLA-A*01:54', 'HLA-A*01:55', 'HLA-A*01:58', 'HLA-A*01:59', 'HLA-A*01:60', 'HLA-A*01:61', 'HLA-A*01:62', 'HLA-A*01:63',
                            'HLA-A*01:64', 'HLA-A*01:65', 'HLA-A*01:66', 'HLA-A*02:01', 'HLA-A*02:02', 'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:05',
                            'HLA-A*02:06', 'HLA-A*02:07', 'HLA-A*02:08', 'HLA-A*02:09', 'HLA-A*02:10', 'HLA-A*02:11', 'HLA-A*02:12', 'HLA-A*02:13',
                            'HLA-A*02:14', 'HLA-A*02:16', 'HLA-A*02:17', 'HLA-A*02:18', 'HLA-A*02:19', 'HLA-A*02:20', 'HLA-A*02:21', 'HLA-A*02:22',
                            'HLA-A*02:24', 'HLA-A*02:25', 'HLA-A*02:26', 'HLA-A*02:27', 'HLA-A*02:28', 'HLA-A*02:29', 'HLA-A*02:30', 'HLA-A*02:31',
                            'HLA-A*02:33', 'HLA-A*02:34', 'HLA-A*02:35', 'HLA-A*02:36', 'HLA-A*02:37', 'HLA-A*02:38', 'HLA-A*02:39', 'HLA-A*02:40',
                            'HLA-A*02:41', 'HLA-A*02:42', 'HLA-A*02:44', 'HLA-A*02:45', 'HLA-A*02:46', 'HLA-A*02:47', 'HLA-A*02:48', 'HLA-A*02:49',
                            'HLA-A*02:50', 'HLA-A*02:51', 'HLA-A*02:52', 'HLA-A*02:54', 'HLA-A*02:55', 'HLA-A*02:56', 'HLA-A*02:57', 'HLA-A*02:58',
                            'HLA-A*02:59', 'HLA-A*02:60', 'HLA-A*02:61', 'HLA-A*02:62', 'HLA-A*02:63', 'HLA-A*02:64', 'HLA-A*02:65', 'HLA-A*02:66',
                            'HLA-A*02:67', 'HLA-A*02:68', 'HLA-A*02:69', 'HLA-A*02:70', 'HLA-A*02:71', 'HLA-A*02:72', 'HLA-A*02:73', 'HLA-A*02:74',
                            'HLA-A*02:75', 'HLA-A*02:76', 'HLA-A*02:77', 'HLA-A*02:78', 'HLA-A*02:79', 'HLA-A*02:80', 'HLA-A*02:81', 'HLA-A*02:84',
                            'HLA-A*02:85', 'HLA-A*02:86', 'HLA-A*02:87', 'HLA-A*02:89', 'HLA-A*02:90', 'HLA-A*02:91', 'HLA-A*02:92', 'HLA-A*02:93',
                            'HLA-A*02:95', 'HLA-A*02:96', 'HLA-A*02:97', 'HLA-A*02:99', 'HLA-A*02:101', 'HLA-A*02:102', 'HLA-A*02:103', 'HLA-A*02:104',
                            'HLA-A*02:105', 'HLA-A*02:106', 'HLA-A*02:107', 'HLA-A*02:108', 'HLA-A*02:109', 'HLA-A*02:110', 'HLA-A*02:111',
                            'HLA-A*02:112', 'HLA-A*02:114', 'HLA-A*02:115', 'HLA-A*02:116', 'HLA-A*02:117', 'HLA-A*02:118', 'HLA-A*02:119',
                            'HLA-A*02:120', 'HLA-A*02:121', 'HLA-A*02:122', 'HLA-A*02:123', 'HLA-A*02:124', 'HLA-A*02:126', 'HLA-A*02:127',
                            'HLA-A*02:128', 'HLA-A*02:129', 'HLA-A*02:130', 'HLA-A*02:131', 'HLA-A*02:132', 'HLA-A*02:133', 'HLA-A*02:134',
                            'HLA-A*02:135', 'HLA-A*02:136', 'HLA-A*02:137', 'HLA-A*02:138', 'HLA-A*02:139', 'HLA-A*02:140', 'HLA-A*02:141',
                            'HLA-A*02:142', 'HLA-A*02:143', 'HLA-A*02:144', 'HLA-A*02:145', 'HLA-A*02:146', 'HLA-A*02:147', 'HLA-A*02:148',
                            'HLA-A*02:149', 'HLA-A*02:150', 'HLA-A*02:151', 'HLA-A*02:152', 'HLA-A*02:153', 'HLA-A*02:154', 'HLA-A*02:155',
                            'HLA-A*02:156', 'HLA-A*02:157', 'HLA-A*02:158', 'HLA-A*02:159', 'HLA-A*02:160', 'HLA-A*02:161', 'HLA-A*02:162',
                            'HLA-A*02:163', 'HLA-A*02:164', 'HLA-A*02:165', 'HLA-A*02:166', 'HLA-A*02:167', 'HLA-A*02:168', 'HLA-A*02:169',
                            'HLA-A*02:170', 'HLA-A*02:171', 'HLA-A*02:172', 'HLA-A*02:173', 'HLA-A*02:174', 'HLA-A*02:175', 'HLA-A*02:176',
                            'HLA-A*02:177', 'HLA-A*02:178', 'HLA-A*02:179', 'HLA-A*02:180', 'HLA-A*02:181', 'HLA-A*02:182', 'HLA-A*02:183',
                            'HLA-A*02:184', 'HLA-A*02:185', 'HLA-A*02:186', 'HLA-A*02:187', 'HLA-A*02:188', 'HLA-A*02:189', 'HLA-A*02:190',
                            'HLA-A*02:191', 'HLA-A*02:192', 'HLA-A*02:193', 'HLA-A*02:194', 'HLA-A*02:195', 'HLA-A*02:196', 'HLA-A*02:197',
                            'HLA-A*02:198', 'HLA-A*02:199', 'HLA-A*02:200', 'HLA-A*02:201', 'HLA-A*02:202', 'HLA-A*02:203', 'HLA-A*02:204',
                            'HLA-A*02:205', 'HLA-A*02:206', 'HLA-A*02:207', 'HLA-A*02:208', 'HLA-A*02:209', 'HLA-A*02:210', 'HLA-A*02:211',
                            'HLA-A*02:212', 'HLA-A*02:213', 'HLA-A*02:214', 'HLA-A*02:215', 'HLA-A*02:216', 'HLA-A*02:217', 'HLA-A*02:218',
                            'HLA-A*02:219', 'HLA-A*02:220', 'HLA-A*02:221', 'HLA-A*02:224', 'HLA-A*02:228', 'HLA-A*02:229', 'HLA-A*02:230',
                            'HLA-A*02:231', 'HLA-A*02:232', 'HLA-A*02:233', 'HLA-A*02:234', 'HLA-A*02:235', 'HLA-A*02:236', 'HLA-A*02:237',
                            'HLA-A*02:238', 'HLA-A*02:239', 'HLA-A*02:240', 'HLA-A*02:241', 'HLA-A*02:242', 'HLA-A*02:243', 'HLA-A*02:244',
                            'HLA-A*02:245', 'HLA-A*02:246', 'HLA-A*02:247', 'HLA-A*02:248', 'HLA-A*02:249', 'HLA-A*02:251', 'HLA-A*02:252',
                            'HLA-A*02:253', 'HLA-A*02:254', 'HLA-A*02:255', 'HLA-A*02:256', 'HLA-A*02:257', 'HLA-A*02:258', 'HLA-A*02:259',
                            'HLA-A*02:260', 'HLA-A*02:261', 'HLA-A*02:262', 'HLA-A*02:263', 'HLA-A*02:264', 'HLA-A*02:265', 'HLA-A*02:266',
                            'HLA-A*03:01', 'HLA-A*03:02', 'HLA-A*03:04', 'HLA-A*03:05', 'HLA-A*03:06', 'HLA-A*03:07', 'HLA-A*03:08', 'HLA-A*03:09',
                            'HLA-A*03:10', 'HLA-A*03:12', 'HLA-A*03:13', 'HLA-A*03:14', 'HLA-A*03:15', 'HLA-A*03:16', 'HLA-A*03:17', 'HLA-A*03:18',
                            'HLA-A*03:19', 'HLA-A*03:20', 'HLA-A*03:22', 'HLA-A*03:23', 'HLA-A*03:24', 'HLA-A*03:25', 'HLA-A*03:26', 'HLA-A*03:27',
                            'HLA-A*03:28', 'HLA-A*03:29', 'HLA-A*03:30', 'HLA-A*03:31', 'HLA-A*03:32', 'HLA-A*03:33', 'HLA-A*03:34', 'HLA-A*03:35',
                            'HLA-A*03:37', 'HLA-A*03:38', 'HLA-A*03:39', 'HLA-A*03:40', 'HLA-A*03:41', 'HLA-A*03:42', 'HLA-A*03:43', 'HLA-A*03:44',
                            'HLA-A*03:45', 'HLA-A*03:46', 'HLA-A*03:47', 'HLA-A*03:48', 'HLA-A*03:49', 'HLA-A*03:50', 'HLA-A*03:51', 'HLA-A*03:52',
                            'HLA-A*03:53', 'HLA-A*03:54', 'HLA-A*03:55', 'HLA-A*03:56', 'HLA-A*03:57', 'HLA-A*03:58', 'HLA-A*03:59', 'HLA-A*03:60',
                            'HLA-A*03:61', 'HLA-A*03:62', 'HLA-A*03:63', 'HLA-A*03:64', 'HLA-A*03:65', 'HLA-A*03:66', 'HLA-A*03:67', 'HLA-A*03:70',
                            'HLA-A*03:71', 'HLA-A*03:72', 'HLA-A*03:73', 'HLA-A*03:74', 'HLA-A*03:75', 'HLA-A*03:76', 'HLA-A*03:77', 'HLA-A*03:78',
                            'HLA-A*03:79', 'HLA-A*03:80', 'HLA-A*03:81', 'HLA-A*03:82', 'HLA-A*11:01', 'HLA-A*11:02', 'HLA-A*11:03', 'HLA-A*11:04',
                            'HLA-A*11:05', 'HLA-A*11:06', 'HLA-A*11:07', 'HLA-A*11:08', 'HLA-A*11:09', 'HLA-A*11:10', 'HLA-A*11:11', 'HLA-A*11:12',
                            'HLA-A*11:13', 'HLA-A*11:14', 'HLA-A*11:15', 'HLA-A*11:16', 'HLA-A*11:17', 'HLA-A*11:18', 'HLA-A*11:19', 'HLA-A*11:20',
                            'HLA-A*11:22', 'HLA-A*11:23', 'HLA-A*11:24', 'HLA-A*11:25', 'HLA-A*11:26', 'HLA-A*11:27', 'HLA-A*11:29', 'HLA-A*11:30',
                            'HLA-A*11:31', 'HLA-A*11:32', 'HLA-A*11:33', 'HLA-A*11:34', 'HLA-A*11:35', 'HLA-A*11:36', 'HLA-A*11:37', 'HLA-A*11:38',
                            'HLA-A*11:39', 'HLA-A*11:40', 'HLA-A*11:41', 'HLA-A*11:42', 'HLA-A*11:43', 'HLA-A*11:44', 'HLA-A*11:45', 'HLA-A*11:46',
                            'HLA-A*11:47', 'HLA-A*11:48', 'HLA-A*11:49', 'HLA-A*11:51', 'HLA-A*11:53', 'HLA-A*11:54', 'HLA-A*11:55', 'HLA-A*11:56',
                            'HLA-A*11:57', 'HLA-A*11:58', 'HLA-A*11:59', 'HLA-A*11:60', 'HLA-A*11:61', 'HLA-A*11:62', 'HLA-A*11:63', 'HLA-A*11:64',
                            'HLA-A*23:01', 'HLA-A*23:02', 'HLA-A*23:03', 'HLA-A*23:04', 'HLA-A*23:05', 'HLA-A*23:06', 'HLA-A*23:09', 'HLA-A*23:10',
                            'HLA-A*23:12', 'HLA-A*23:13', 'HLA-A*23:14', 'HLA-A*23:15', 'HLA-A*23:16', 'HLA-A*23:17', 'HLA-A*23:18', 'HLA-A*23:20',
                            'HLA-A*23:21', 'HLA-A*23:22', 'HLA-A*23:23', 'HLA-A*23:24', 'HLA-A*23:25', 'HLA-A*23:26', 'HLA-A*24:02', 'HLA-A*24:03',
                            'HLA-A*24:04', 'HLA-A*24:05', 'HLA-A*24:06', 'HLA-A*24:07', 'HLA-A*24:08', 'HLA-A*24:10', 'HLA-A*24:13', 'HLA-A*24:14',
                            'HLA-A*24:15', 'HLA-A*24:17', 'HLA-A*24:18', 'HLA-A*24:19', 'HLA-A*24:20', 'HLA-A*24:21', 'HLA-A*24:22', 'HLA-A*24:23',
                            'HLA-A*24:24', 'HLA-A*24:25', 'HLA-A*24:26', 'HLA-A*24:27', 'HLA-A*24:28', 'HLA-A*24:29', 'HLA-A*24:30', 'HLA-A*24:31',
                            'HLA-A*24:32', 'HLA-A*24:33', 'HLA-A*24:34', 'HLA-A*24:35', 'HLA-A*24:37', 'HLA-A*24:38', 'HLA-A*24:39', 'HLA-A*24:41',
                            'HLA-A*24:42', 'HLA-A*24:43', 'HLA-A*24:44', 'HLA-A*24:46', 'HLA-A*24:47', 'HLA-A*24:49', 'HLA-A*24:50', 'HLA-A*24:51',
                            'HLA-A*24:52', 'HLA-A*24:53', 'HLA-A*24:54', 'HLA-A*24:55', 'HLA-A*24:56', 'HLA-A*24:57', 'HLA-A*24:58', 'HLA-A*24:59',
                            'HLA-A*24:61', 'HLA-A*24:62', 'HLA-A*24:63', 'HLA-A*24:64', 'HLA-A*24:66', 'HLA-A*24:67', 'HLA-A*24:68', 'HLA-A*24:69',
                            'HLA-A*24:70', 'HLA-A*24:71', 'HLA-A*24:72', 'HLA-A*24:73', 'HLA-A*24:74', 'HLA-A*24:75', 'HLA-A*24:76', 'HLA-A*24:77',
                            'HLA-A*24:78', 'HLA-A*24:79', 'HLA-A*24:80', 'HLA-A*24:81', 'HLA-A*24:82', 'HLA-A*24:85', 'HLA-A*24:87', 'HLA-A*24:88',
                            'HLA-A*24:89', 'HLA-A*24:91', 'HLA-A*24:92', 'HLA-A*24:93', 'HLA-A*24:94', 'HLA-A*24:95', 'HLA-A*24:96', 'HLA-A*24:97',
                            'HLA-A*24:98', 'HLA-A*24:99', 'HLA-A*24:100', 'HLA-A*24:101', 'HLA-A*24:102', 'HLA-A*24:103', 'HLA-A*24:104',
                            'HLA-A*24:105', 'HLA-A*24:106', 'HLA-A*24:107', 'HLA-A*24:108', 'HLA-A*24:109', 'HLA-A*24:110', 'HLA-A*24:111',
                            'HLA-A*24:112', 'HLA-A*24:113', 'HLA-A*24:114', 'HLA-A*24:115', 'HLA-A*24:116', 'HLA-A*24:117', 'HLA-A*24:118',
                            'HLA-A*24:119', 'HLA-A*24:120', 'HLA-A*24:121', 'HLA-A*24:122', 'HLA-A*24:123', 'HLA-A*24:124', 'HLA-A*24:125',
                            'HLA-A*24:126', 'HLA-A*24:127', 'HLA-A*24:128', 'HLA-A*24:129', 'HLA-A*24:130', 'HLA-A*24:131', 'HLA-A*24:133',
                            'HLA-A*24:134', 'HLA-A*24:135', 'HLA-A*24:136', 'HLA-A*24:137', 'HLA-A*24:138', 'HLA-A*24:139', 'HLA-A*24:140',
                            'HLA-A*24:141', 'HLA-A*24:142', 'HLA-A*24:143', 'HLA-A*24:144', 'HLA-A*25:01', 'HLA-A*25:02', 'HLA-A*25:03', 'HLA-A*25:04',
                            'HLA-A*25:05', 'HLA-A*25:06', 'HLA-A*25:07', 'HLA-A*25:08', 'HLA-A*25:09', 'HLA-A*25:10', 'HLA-A*25:11', 'HLA-A*25:13',
                            'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*26:04', 'HLA-A*26:05', 'HLA-A*26:06', 'HLA-A*26:07', 'HLA-A*26:08',
                            'HLA-A*26:09', 'HLA-A*26:10', 'HLA-A*26:12', 'HLA-A*26:13', 'HLA-A*26:14', 'HLA-A*26:15', 'HLA-A*26:16', 'HLA-A*26:17',
                            'HLA-A*26:18', 'HLA-A*26:19', 'HLA-A*26:20', 'HLA-A*26:21', 'HLA-A*26:22', 'HLA-A*26:23', 'HLA-A*26:24', 'HLA-A*26:26',
                            'HLA-A*26:27', 'HLA-A*26:28', 'HLA-A*26:29', 'HLA-A*26:30', 'HLA-A*26:31', 'HLA-A*26:32', 'HLA-A*26:33', 'HLA-A*26:34',
                            'HLA-A*26:35', 'HLA-A*26:36', 'HLA-A*26:37', 'HLA-A*26:38', 'HLA-A*26:39', 'HLA-A*26:40', 'HLA-A*26:41', 'HLA-A*26:42',
                            'HLA-A*26:43', 'HLA-A*26:45', 'HLA-A*26:46', 'HLA-A*26:47', 'HLA-A*26:48', 'HLA-A*26:49', 'HLA-A*26:50', 'HLA-A*29:01',
                            'HLA-A*29:02', 'HLA-A*29:03', 'HLA-A*29:04', 'HLA-A*29:05', 'HLA-A*29:06', 'HLA-A*29:07', 'HLA-A*29:09', 'HLA-A*29:10',
                            'HLA-A*29:11', 'HLA-A*29:12', 'HLA-A*29:13', 'HLA-A*29:14', 'HLA-A*29:15', 'HLA-A*29:16', 'HLA-A*29:17', 'HLA-A*29:18',
                            'HLA-A*29:19', 'HLA-A*29:20', 'HLA-A*29:21', 'HLA-A*29:22', 'HLA-A*30:01', 'HLA-A*30:02', 'HLA-A*30:03', 'HLA-A*30:04',
                            'HLA-A*30:06', 'HLA-A*30:07', 'HLA-A*30:08', 'HLA-A*30:09', 'HLA-A*30:10', 'HLA-A*30:11', 'HLA-A*30:12', 'HLA-A*30:13',
                            'HLA-A*30:15', 'HLA-A*30:16', 'HLA-A*30:17', 'HLA-A*30:18', 'HLA-A*30:19', 'HLA-A*30:20', 'HLA-A*30:22', 'HLA-A*30:23',
                            'HLA-A*30:24', 'HLA-A*30:25', 'HLA-A*30:26', 'HLA-A*30:28', 'HLA-A*30:29', 'HLA-A*30:30', 'HLA-A*30:31', 'HLA-A*30:32',
                            'HLA-A*30:33', 'HLA-A*30:34', 'HLA-A*30:35', 'HLA-A*30:36', 'HLA-A*30:37', 'HLA-A*30:38', 'HLA-A*30:39', 'HLA-A*30:40',
                            'HLA-A*30:41', 'HLA-A*31:01', 'HLA-A*31:02', 'HLA-A*31:03', 'HLA-A*31:04', 'HLA-A*31:05', 'HLA-A*31:06', 'HLA-A*31:07',
                            'HLA-A*31:08', 'HLA-A*31:09', 'HLA-A*31:10', 'HLA-A*31:11', 'HLA-A*31:12', 'HLA-A*31:13', 'HLA-A*31:15', 'HLA-A*31:16',
                            'HLA-A*31:17', 'HLA-A*31:18', 'HLA-A*31:19', 'HLA-A*31:20', 'HLA-A*31:21', 'HLA-A*31:22', 'HLA-A*31:23', 'HLA-A*31:24',
                            'HLA-A*31:25', 'HLA-A*31:26', 'HLA-A*31:27', 'HLA-A*31:28', 'HLA-A*31:29', 'HLA-A*31:30', 'HLA-A*31:31', 'HLA-A*31:32',
                            'HLA-A*31:33', 'HLA-A*31:34', 'HLA-A*31:35', 'HLA-A*31:36', 'HLA-A*31:37', 'HLA-A*32:01', 'HLA-A*32:02', 'HLA-A*32:03',
                            'HLA-A*32:04', 'HLA-A*32:05', 'HLA-A*32:06', 'HLA-A*32:07', 'HLA-A*32:08', 'HLA-A*32:09', 'HLA-A*32:10', 'HLA-A*32:12',
                            'HLA-A*32:13', 'HLA-A*32:14', 'HLA-A*32:15', 'HLA-A*32:16', 'HLA-A*32:17', 'HLA-A*32:18', 'HLA-A*32:20', 'HLA-A*32:21',
                            'HLA-A*32:22', 'HLA-A*32:23', 'HLA-A*32:24', 'HLA-A*32:25', 'HLA-A*33:01', 'HLA-A*33:03', 'HLA-A*33:04', 'HLA-A*33:05',
                            'HLA-A*33:06', 'HLA-A*33:07', 'HLA-A*33:08', 'HLA-A*33:09', 'HLA-A*33:10', 'HLA-A*33:11', 'HLA-A*33:12', 'HLA-A*33:13',
                            'HLA-A*33:14', 'HLA-A*33:15', 'HLA-A*33:16', 'HLA-A*33:17', 'HLA-A*33:18', 'HLA-A*33:19', 'HLA-A*33:20', 'HLA-A*33:21',
                            'HLA-A*33:22', 'HLA-A*33:23', 'HLA-A*33:24', 'HLA-A*33:25', 'HLA-A*33:26', 'HLA-A*33:27', 'HLA-A*33:28', 'HLA-A*33:29',
                            'HLA-A*33:30', 'HLA-A*33:31', 'HLA-A*34:01', 'HLA-A*34:02', 'HLA-A*34:03', 'HLA-A*34:04', 'HLA-A*34:05', 'HLA-A*34:06',
                            'HLA-A*34:07', 'HLA-A*34:08', 'HLA-A*36:01', 'HLA-A*36:02', 'HLA-A*36:03', 'HLA-A*36:04', 'HLA-A*36:05', 'HLA-A*43:01',
                            'HLA-A*66:01', 'HLA-A*66:02', 'HLA-A*66:03', 'HLA-A*66:04', 'HLA-A*66:05', 'HLA-A*66:06', 'HLA-A*66:07', 'HLA-A*66:08',
                            'HLA-A*66:09', 'HLA-A*66:10', 'HLA-A*66:11', 'HLA-A*66:12', 'HLA-A*66:13', 'HLA-A*66:14', 'HLA-A*66:15', 'HLA-A*68:01',
                            'HLA-A*68:02', 'HLA-A*68:03', 'HLA-A*68:04', 'HLA-A*68:05', 'HLA-A*68:06', 'HLA-A*68:07', 'HLA-A*68:08', 'HLA-A*68:09',
                            'HLA-A*68:10', 'HLA-A*68:12', 'HLA-A*68:13', 'HLA-A*68:14', 'HLA-A*68:15', 'HLA-A*68:16', 'HLA-A*68:17', 'HLA-A*68:19',
                            'HLA-A*68:20', 'HLA-A*68:21', 'HLA-A*68:22', 'HLA-A*68:23', 'HLA-A*68:24', 'HLA-A*68:25', 'HLA-A*68:26', 'HLA-A*68:27',
                            'HLA-A*68:28', 'HLA-A*68:29', 'HLA-A*68:30', 'HLA-A*68:31', 'HLA-A*68:32', 'HLA-A*68:33', 'HLA-A*68:34', 'HLA-A*68:35',
                            'HLA-A*68:36', 'HLA-A*68:37', 'HLA-A*68:38', 'HLA-A*68:39', 'HLA-A*68:40', 'HLA-A*68:41', 'HLA-A*68:42', 'HLA-A*68:43',
                            'HLA-A*68:44', 'HLA-A*68:45', 'HLA-A*68:46', 'HLA-A*68:47', 'HLA-A*68:48', 'HLA-A*68:50', 'HLA-A*68:51', 'HLA-A*68:52',
                            'HLA-A*68:53', 'HLA-A*68:54', 'HLA-A*69:01', 'HLA-A*74:01', 'HLA-A*74:02', 'HLA-A*74:03', 'HLA-A*74:04', 'HLA-A*74:05',
                            'HLA-A*74:06', 'HLA-A*74:07', 'HLA-A*74:08', 'HLA-A*74:09', 'HLA-A*74:10', 'HLA-A*74:11', 'HLA-A*74:13', 'HLA-A*80:01',
                            'HLA-A*80:02', 'HLA-B*07:02', 'HLA-B*07:03', 'HLA-B*07:04', 'HLA-B*07:05', 'HLA-B*07:06', 'HLA-B*07:07', 'HLA-B*07:08',
                            'HLA-B*07:09', 'HLA-B*07:10', 'HLA-B*07:11', 'HLA-B*07:12', 'HLA-B*07:13', 'HLA-B*07:14', 'HLA-B*07:15', 'HLA-B*07:16',
                            'HLA-B*07:17', 'HLA-B*07:18', 'HLA-B*07:19', 'HLA-B*07:20', 'HLA-B*07:21', 'HLA-B*07:22', 'HLA-B*07:23', 'HLA-B*07:24',
                            'HLA-B*07:25', 'HLA-B*07:26', 'HLA-B*07:27', 'HLA-B*07:28', 'HLA-B*07:29', 'HLA-B*07:30', 'HLA-B*07:31', 'HLA-B*07:32',
                            'HLA-B*07:33', 'HLA-B*07:34', 'HLA-B*07:35', 'HLA-B*07:36', 'HLA-B*07:37', 'HLA-B*07:38', 'HLA-B*07:39', 'HLA-B*07:40',
                            'HLA-B*07:41', 'HLA-B*07:42', 'HLA-B*07:43', 'HLA-B*07:44', 'HLA-B*07:45', 'HLA-B*07:46', 'HLA-B*07:47', 'HLA-B*07:48',
                            'HLA-B*07:50', 'HLA-B*07:51', 'HLA-B*07:52', 'HLA-B*07:53', 'HLA-B*07:54', 'HLA-B*07:55', 'HLA-B*07:56', 'HLA-B*07:57',
                            'HLA-B*07:58', 'HLA-B*07:59', 'HLA-B*07:60', 'HLA-B*07:61', 'HLA-B*07:62', 'HLA-B*07:63', 'HLA-B*07:64', 'HLA-B*07:65',
                            'HLA-B*07:66', 'HLA-B*07:68', 'HLA-B*07:69', 'HLA-B*07:70', 'HLA-B*07:71', 'HLA-B*07:72', 'HLA-B*07:73', 'HLA-B*07:74',
                            'HLA-B*07:75', 'HLA-B*07:76', 'HLA-B*07:77', 'HLA-B*07:78', 'HLA-B*07:79', 'HLA-B*07:80', 'HLA-B*07:81', 'HLA-B*07:82',
                            'HLA-B*07:83', 'HLA-B*07:84', 'HLA-B*07:85', 'HLA-B*07:86', 'HLA-B*07:87', 'HLA-B*07:88', 'HLA-B*07:89', 'HLA-B*07:90',
                            'HLA-B*07:91', 'HLA-B*07:92', 'HLA-B*07:93', 'HLA-B*07:94', 'HLA-B*07:95', 'HLA-B*07:96', 'HLA-B*07:97', 'HLA-B*07:98',
                            'HLA-B*07:99', 'HLA-B*07:100', 'HLA-B*07:101', 'HLA-B*07:102', 'HLA-B*07:103', 'HLA-B*07:104', 'HLA-B*07:105',
                            'HLA-B*07:106', 'HLA-B*07:107', 'HLA-B*07:108', 'HLA-B*07:109', 'HLA-B*07:110', 'HLA-B*07:112', 'HLA-B*07:113',
                            'HLA-B*07:114', 'HLA-B*07:115', 'HLA-B*08:01', 'HLA-B*08:02', 'HLA-B*08:03', 'HLA-B*08:04', 'HLA-B*08:05', 'HLA-B*08:07',
                            'HLA-B*08:09', 'HLA-B*08:10', 'HLA-B*08:11', 'HLA-B*08:12', 'HLA-B*08:13', 'HLA-B*08:14', 'HLA-B*08:15', 'HLA-B*08:16',
                            'HLA-B*08:17', 'HLA-B*08:18', 'HLA-B*08:20', 'HLA-B*08:21', 'HLA-B*08:22', 'HLA-B*08:23', 'HLA-B*08:24', 'HLA-B*08:25',
                            'HLA-B*08:26', 'HLA-B*08:27', 'HLA-B*08:28', 'HLA-B*08:29', 'HLA-B*08:31', 'HLA-B*08:32', 'HLA-B*08:33', 'HLA-B*08:34',
                            'HLA-B*08:35', 'HLA-B*08:36', 'HLA-B*08:37', 'HLA-B*08:38', 'HLA-B*08:39', 'HLA-B*08:40', 'HLA-B*08:41', 'HLA-B*08:42',
                            'HLA-B*08:43', 'HLA-B*08:44', 'HLA-B*08:45', 'HLA-B*08:46', 'HLA-B*08:47', 'HLA-B*08:48', 'HLA-B*08:49', 'HLA-B*08:50',
                            'HLA-B*08:51', 'HLA-B*08:52', 'HLA-B*08:53', 'HLA-B*08:54', 'HLA-B*08:55', 'HLA-B*08:56', 'HLA-B*08:57', 'HLA-B*08:58',
                            'HLA-B*08:59', 'HLA-B*08:60', 'HLA-B*08:61', 'HLA-B*08:62', 'HLA-B*13:01', 'HLA-B*13:02', 'HLA-B*13:03', 'HLA-B*13:04',
                            'HLA-B*13:06', 'HLA-B*13:09', 'HLA-B*13:10', 'HLA-B*13:11', 'HLA-B*13:12', 'HLA-B*13:13', 'HLA-B*13:14', 'HLA-B*13:15',
                            'HLA-B*13:16', 'HLA-B*13:17', 'HLA-B*13:18', 'HLA-B*13:19', 'HLA-B*13:20', 'HLA-B*13:21', 'HLA-B*13:22', 'HLA-B*13:23',
                            'HLA-B*13:25', 'HLA-B*13:26', 'HLA-B*13:27', 'HLA-B*13:28', 'HLA-B*13:29', 'HLA-B*13:30', 'HLA-B*13:31', 'HLA-B*13:32',
                            'HLA-B*13:33', 'HLA-B*13:34', 'HLA-B*13:35', 'HLA-B*13:36', 'HLA-B*13:37', 'HLA-B*13:38', 'HLA-B*13:39', 'HLA-B*14:01',
                            'HLA-B*14:02', 'HLA-B*14:03', 'HLA-B*14:04', 'HLA-B*14:05', 'HLA-B*14:06', 'HLA-B*14:08', 'HLA-B*14:09', 'HLA-B*14:10',
                            'HLA-B*14:11', 'HLA-B*14:12', 'HLA-B*14:13', 'HLA-B*14:14', 'HLA-B*14:15', 'HLA-B*14:16', 'HLA-B*14:17', 'HLA-B*14:18',
                            'HLA-B*15:01', 'HLA-B*15:02', 'HLA-B*15:03', 'HLA-B*15:04', 'HLA-B*15:05', 'HLA-B*15:06', 'HLA-B*15:07', 'HLA-B*15:08',
                            'HLA-B*15:09', 'HLA-B*15:10', 'HLA-B*15:11', 'HLA-B*15:12', 'HLA-B*15:13', 'HLA-B*15:14', 'HLA-B*15:15', 'HLA-B*15:16',
                            'HLA-B*15:17', 'HLA-B*15:18', 'HLA-B*15:19', 'HLA-B*15:20', 'HLA-B*15:21', 'HLA-B*15:23', 'HLA-B*15:24', 'HLA-B*15:25',
                            'HLA-B*15:27', 'HLA-B*15:28', 'HLA-B*15:29', 'HLA-B*15:30', 'HLA-B*15:31', 'HLA-B*15:32', 'HLA-B*15:33', 'HLA-B*15:34',
                            'HLA-B*15:35', 'HLA-B*15:36', 'HLA-B*15:37', 'HLA-B*15:38', 'HLA-B*15:39', 'HLA-B*15:40', 'HLA-B*15:42', 'HLA-B*15:43',
                            'HLA-B*15:44', 'HLA-B*15:45', 'HLA-B*15:46', 'HLA-B*15:47', 'HLA-B*15:48', 'HLA-B*15:49', 'HLA-B*15:50', 'HLA-B*15:51',
                            'HLA-B*15:52', 'HLA-B*15:53', 'HLA-B*15:54', 'HLA-B*15:55', 'HLA-B*15:56', 'HLA-B*15:57', 'HLA-B*15:58', 'HLA-B*15:60',
                            'HLA-B*15:61', 'HLA-B*15:62', 'HLA-B*15:63', 'HLA-B*15:64', 'HLA-B*15:65', 'HLA-B*15:66', 'HLA-B*15:67', 'HLA-B*15:68',
                            'HLA-B*15:69', 'HLA-B*15:70', 'HLA-B*15:71', 'HLA-B*15:72', 'HLA-B*15:73', 'HLA-B*15:74', 'HLA-B*15:75', 'HLA-B*15:76',
                            'HLA-B*15:77', 'HLA-B*15:78', 'HLA-B*15:80', 'HLA-B*15:81', 'HLA-B*15:82', 'HLA-B*15:83', 'HLA-B*15:84', 'HLA-B*15:85',
                            'HLA-B*15:86', 'HLA-B*15:87', 'HLA-B*15:88', 'HLA-B*15:89', 'HLA-B*15:90', 'HLA-B*15:91', 'HLA-B*15:92', 'HLA-B*15:93',
                            'HLA-B*15:95', 'HLA-B*15:96', 'HLA-B*15:97', 'HLA-B*15:98', 'HLA-B*15:99', 'HLA-B*15:101', 'HLA-B*15:102', 'HLA-B*15:103',
                            'HLA-B*15:104', 'HLA-B*15:105', 'HLA-B*15:106', 'HLA-B*15:107', 'HLA-B*15:108', 'HLA-B*15:109', 'HLA-B*15:110',
                            'HLA-B*15:112', 'HLA-B*15:113', 'HLA-B*15:114', 'HLA-B*15:115', 'HLA-B*15:116', 'HLA-B*15:117', 'HLA-B*15:118',
                            'HLA-B*15:119', 'HLA-B*15:120', 'HLA-B*15:121', 'HLA-B*15:122', 'HLA-B*15:123', 'HLA-B*15:124', 'HLA-B*15:125',
                            'HLA-B*15:126', 'HLA-B*15:127', 'HLA-B*15:128', 'HLA-B*15:129', 'HLA-B*15:131', 'HLA-B*15:132', 'HLA-B*15:133',
                            'HLA-B*15:134', 'HLA-B*15:135', 'HLA-B*15:136', 'HLA-B*15:137', 'HLA-B*15:138', 'HLA-B*15:139', 'HLA-B*15:140',
                            'HLA-B*15:141', 'HLA-B*15:142', 'HLA-B*15:143', 'HLA-B*15:144', 'HLA-B*15:145', 'HLA-B*15:146', 'HLA-B*15:147',
                            'HLA-B*15:148', 'HLA-B*15:150', 'HLA-B*15:151', 'HLA-B*15:152', 'HLA-B*15:153', 'HLA-B*15:154', 'HLA-B*15:155',
                            'HLA-B*15:156', 'HLA-B*15:157', 'HLA-B*15:158', 'HLA-B*15:159', 'HLA-B*15:160', 'HLA-B*15:161', 'HLA-B*15:162',
                            'HLA-B*15:163', 'HLA-B*15:164', 'HLA-B*15:165', 'HLA-B*15:166', 'HLA-B*15:167', 'HLA-B*15:168', 'HLA-B*15:169',
                            'HLA-B*15:170', 'HLA-B*15:171', 'HLA-B*15:172', 'HLA-B*15:173', 'HLA-B*15:174', 'HLA-B*15:175', 'HLA-B*15:176',
                            'HLA-B*15:177', 'HLA-B*15:178', 'HLA-B*15:179', 'HLA-B*15:180', 'HLA-B*15:183', 'HLA-B*15:184', 'HLA-B*15:185',
                            'HLA-B*15:186', 'HLA-B*15:187', 'HLA-B*15:188', 'HLA-B*15:189', 'HLA-B*15:191', 'HLA-B*15:192', 'HLA-B*15:193',
                            'HLA-B*15:194', 'HLA-B*15:195', 'HLA-B*15:196', 'HLA-B*15:197', 'HLA-B*15:198', 'HLA-B*15:199', 'HLA-B*15:200',
                            'HLA-B*15:201', 'HLA-B*15:202', 'HLA-B*18:01', 'HLA-B*18:02', 'HLA-B*18:03', 'HLA-B*18:04', 'HLA-B*18:05', 'HLA-B*18:06',
                            'HLA-B*18:07', 'HLA-B*18:08', 'HLA-B*18:09', 'HLA-B*18:10', 'HLA-B*18:11', 'HLA-B*18:12', 'HLA-B*18:13', 'HLA-B*18:14',
                            'HLA-B*18:15', 'HLA-B*18:18', 'HLA-B*18:19', 'HLA-B*18:20', 'HLA-B*18:21', 'HLA-B*18:22', 'HLA-B*18:24', 'HLA-B*18:25',
                            'HLA-B*18:26', 'HLA-B*18:27', 'HLA-B*18:28', 'HLA-B*18:29', 'HLA-B*18:30', 'HLA-B*18:31', 'HLA-B*18:32', 'HLA-B*18:33',
                            'HLA-B*18:34', 'HLA-B*18:35', 'HLA-B*18:36', 'HLA-B*18:37', 'HLA-B*18:38', 'HLA-B*18:39', 'HLA-B*18:40', 'HLA-B*18:41',
                            'HLA-B*18:42', 'HLA-B*18:43', 'HLA-B*18:44', 'HLA-B*18:45', 'HLA-B*18:46', 'HLA-B*18:47', 'HLA-B*18:48', 'HLA-B*18:49',
                            'HLA-B*18:50', 'HLA-B*27:01', 'HLA-B*27:02', 'HLA-B*27:03', 'HLA-B*27:04', 'HLA-B*27:05', 'HLA-B*27:06', 'HLA-B*27:07',
                            'HLA-B*27:08', 'HLA-B*27:09', 'HLA-B*27:10', 'HLA-B*27:11', 'HLA-B*27:12', 'HLA-B*27:13', 'HLA-B*27:14', 'HLA-B*27:15',
                            'HLA-B*27:16', 'HLA-B*27:17', 'HLA-B*27:18', 'HLA-B*27:19', 'HLA-B*27:20', 'HLA-B*27:21', 'HLA-B*27:23', 'HLA-B*27:24',
                            'HLA-B*27:25', 'HLA-B*27:26', 'HLA-B*27:27', 'HLA-B*27:28', 'HLA-B*27:29', 'HLA-B*27:30', 'HLA-B*27:31', 'HLA-B*27:32',
                            'HLA-B*27:33', 'HLA-B*27:34', 'HLA-B*27:35', 'HLA-B*27:36', 'HLA-B*27:37', 'HLA-B*27:38', 'HLA-B*27:39', 'HLA-B*27:40',
                            'HLA-B*27:41', 'HLA-B*27:42', 'HLA-B*27:43', 'HLA-B*27:44', 'HLA-B*27:45', 'HLA-B*27:46', 'HLA-B*27:47', 'HLA-B*27:48',
                            'HLA-B*27:49', 'HLA-B*27:50', 'HLA-B*27:51', 'HLA-B*27:52', 'HLA-B*27:53', 'HLA-B*27:54', 'HLA-B*27:55', 'HLA-B*27:56',
                            'HLA-B*27:57', 'HLA-B*27:58', 'HLA-B*27:60', 'HLA-B*27:61', 'HLA-B*27:62', 'HLA-B*27:63', 'HLA-B*27:67', 'HLA-B*27:68',
                            'HLA-B*27:69', 'HLA-B*35:01', 'HLA-B*35:02', 'HLA-B*35:03', 'HLA-B*35:04', 'HLA-B*35:05', 'HLA-B*35:06', 'HLA-B*35:07',
                            'HLA-B*35:08', 'HLA-B*35:09', 'HLA-B*35:10', 'HLA-B*35:11', 'HLA-B*35:12', 'HLA-B*35:13', 'HLA-B*35:14', 'HLA-B*35:15',
                            'HLA-B*35:16', 'HLA-B*35:17', 'HLA-B*35:18', 'HLA-B*35:19', 'HLA-B*35:20', 'HLA-B*35:21', 'HLA-B*35:22', 'HLA-B*35:23',
                            'HLA-B*35:24', 'HLA-B*35:25', 'HLA-B*35:26', 'HLA-B*35:27', 'HLA-B*35:28', 'HLA-B*35:29', 'HLA-B*35:30', 'HLA-B*35:31',
                            'HLA-B*35:32', 'HLA-B*35:33', 'HLA-B*35:34', 'HLA-B*35:35', 'HLA-B*35:36', 'HLA-B*35:37', 'HLA-B*35:38', 'HLA-B*35:39',
                            'HLA-B*35:41', 'HLA-B*35:42', 'HLA-B*35:43', 'HLA-B*35:44', 'HLA-B*35:45', 'HLA-B*35:46', 'HLA-B*35:47', 'HLA-B*35:48',
                            'HLA-B*35:49', 'HLA-B*35:50', 'HLA-B*35:51', 'HLA-B*35:52', 'HLA-B*35:54', 'HLA-B*35:55', 'HLA-B*35:56', 'HLA-B*35:57',
                            'HLA-B*35:58', 'HLA-B*35:59', 'HLA-B*35:60', 'HLA-B*35:61', 'HLA-B*35:62', 'HLA-B*35:63', 'HLA-B*35:64', 'HLA-B*35:66',
                            'HLA-B*35:67', 'HLA-B*35:68', 'HLA-B*35:69', 'HLA-B*35:70', 'HLA-B*35:71', 'HLA-B*35:72', 'HLA-B*35:74', 'HLA-B*35:75',
                            'HLA-B*35:76', 'HLA-B*35:77', 'HLA-B*35:78', 'HLA-B*35:79', 'HLA-B*35:80', 'HLA-B*35:81', 'HLA-B*35:82', 'HLA-B*35:83',
                            'HLA-B*35:84', 'HLA-B*35:85', 'HLA-B*35:86', 'HLA-B*35:87', 'HLA-B*35:88', 'HLA-B*35:89', 'HLA-B*35:90', 'HLA-B*35:91',
                            'HLA-B*35:92', 'HLA-B*35:93', 'HLA-B*35:94', 'HLA-B*35:95', 'HLA-B*35:96', 'HLA-B*35:97', 'HLA-B*35:98', 'HLA-B*35:99',
                            'HLA-B*35:100', 'HLA-B*35:101', 'HLA-B*35:102', 'HLA-B*35:103', 'HLA-B*35:104', 'HLA-B*35:105', 'HLA-B*35:106',
                            'HLA-B*35:107', 'HLA-B*35:108', 'HLA-B*35:109', 'HLA-B*35:110', 'HLA-B*35:111', 'HLA-B*35:112', 'HLA-B*35:113',
                            'HLA-B*35:114', 'HLA-B*35:115', 'HLA-B*35:116', 'HLA-B*35:117', 'HLA-B*35:118', 'HLA-B*35:119', 'HLA-B*35:120',
                            'HLA-B*35:121', 'HLA-B*35:122', 'HLA-B*35:123', 'HLA-B*35:124', 'HLA-B*35:125', 'HLA-B*35:126', 'HLA-B*35:127',
                            'HLA-B*35:128', 'HLA-B*35:131', 'HLA-B*35:132', 'HLA-B*35:133', 'HLA-B*35:135', 'HLA-B*35:136', 'HLA-B*35:137',
                            'HLA-B*35:138', 'HLA-B*35:139', 'HLA-B*35:140', 'HLA-B*35:141', 'HLA-B*35:142', 'HLA-B*35:143', 'HLA-B*35:144',
                            'HLA-B*37:01', 'HLA-B*37:02', 'HLA-B*37:04', 'HLA-B*37:05', 'HLA-B*37:06', 'HLA-B*37:07', 'HLA-B*37:08', 'HLA-B*37:09',
                            'HLA-B*37:10', 'HLA-B*37:11', 'HLA-B*37:12', 'HLA-B*37:13', 'HLA-B*37:14', 'HLA-B*37:15', 'HLA-B*37:17', 'HLA-B*37:18',
                            'HLA-B*37:19', 'HLA-B*37:20', 'HLA-B*37:21', 'HLA-B*37:22', 'HLA-B*37:23', 'HLA-B*38:01', 'HLA-B*38:02', 'HLA-B*38:03',
                            'HLA-B*38:04', 'HLA-B*38:05', 'HLA-B*38:06', 'HLA-B*38:07', 'HLA-B*38:08', 'HLA-B*38:09', 'HLA-B*38:10', 'HLA-B*38:11',
                            'HLA-B*38:12', 'HLA-B*38:13', 'HLA-B*38:14', 'HLA-B*38:15', 'HLA-B*38:16', 'HLA-B*38:17', 'HLA-B*38:18', 'HLA-B*38:19',
                            'HLA-B*38:20', 'HLA-B*38:21', 'HLA-B*38:22', 'HLA-B*38:23', 'HLA-B*39:01', 'HLA-B*39:02', 'HLA-B*39:03', 'HLA-B*39:04',
                            'HLA-B*39:05', 'HLA-B*39:06', 'HLA-B*39:07', 'HLA-B*39:08', 'HLA-B*39:09', 'HLA-B*39:10', 'HLA-B*39:11', 'HLA-B*39:12',
                            'HLA-B*39:13', 'HLA-B*39:14', 'HLA-B*39:15', 'HLA-B*39:16', 'HLA-B*39:17', 'HLA-B*39:18', 'HLA-B*39:19', 'HLA-B*39:20',
                            'HLA-B*39:22', 'HLA-B*39:23', 'HLA-B*39:24', 'HLA-B*39:26', 'HLA-B*39:27', 'HLA-B*39:28', 'HLA-B*39:29', 'HLA-B*39:30',
                            'HLA-B*39:31', 'HLA-B*39:32', 'HLA-B*39:33', 'HLA-B*39:34', 'HLA-B*39:35', 'HLA-B*39:36', 'HLA-B*39:37', 'HLA-B*39:39',
                            'HLA-B*39:41', 'HLA-B*39:42', 'HLA-B*39:43', 'HLA-B*39:44', 'HLA-B*39:45', 'HLA-B*39:46', 'HLA-B*39:47', 'HLA-B*39:48',
                            'HLA-B*39:49', 'HLA-B*39:50', 'HLA-B*39:51', 'HLA-B*39:52', 'HLA-B*39:53', 'HLA-B*39:54', 'HLA-B*39:55', 'HLA-B*39:56',
                            'HLA-B*39:57', 'HLA-B*39:58', 'HLA-B*39:59', 'HLA-B*39:60', 'HLA-B*40:01', 'HLA-B*40:02', 'HLA-B*40:03', 'HLA-B*40:04',
                            'HLA-B*40:05', 'HLA-B*40:06', 'HLA-B*40:07', 'HLA-B*40:08', 'HLA-B*40:09', 'HLA-B*40:10', 'HLA-B*40:11', 'HLA-B*40:12',
                            'HLA-B*40:13', 'HLA-B*40:14', 'HLA-B*40:15', 'HLA-B*40:16', 'HLA-B*40:18', 'HLA-B*40:19', 'HLA-B*40:20', 'HLA-B*40:21',
                            'HLA-B*40:23', 'HLA-B*40:24', 'HLA-B*40:25', 'HLA-B*40:26', 'HLA-B*40:27', 'HLA-B*40:28', 'HLA-B*40:29', 'HLA-B*40:30',
                            'HLA-B*40:31', 'HLA-B*40:32', 'HLA-B*40:33', 'HLA-B*40:34', 'HLA-B*40:35', 'HLA-B*40:36', 'HLA-B*40:37', 'HLA-B*40:38',
                            'HLA-B*40:39', 'HLA-B*40:40', 'HLA-B*40:42', 'HLA-B*40:43', 'HLA-B*40:44', 'HLA-B*40:45', 'HLA-B*40:46', 'HLA-B*40:47',
                            'HLA-B*40:48', 'HLA-B*40:49', 'HLA-B*40:50', 'HLA-B*40:51', 'HLA-B*40:52', 'HLA-B*40:53', 'HLA-B*40:54', 'HLA-B*40:55',
                            'HLA-B*40:56', 'HLA-B*40:57', 'HLA-B*40:58', 'HLA-B*40:59', 'HLA-B*40:60', 'HLA-B*40:61', 'HLA-B*40:62', 'HLA-B*40:63',
                            'HLA-B*40:64', 'HLA-B*40:65', 'HLA-B*40:66', 'HLA-B*40:67', 'HLA-B*40:68', 'HLA-B*40:69', 'HLA-B*40:70', 'HLA-B*40:71',
                            'HLA-B*40:72', 'HLA-B*40:73', 'HLA-B*40:74', 'HLA-B*40:75', 'HLA-B*40:76', 'HLA-B*40:77', 'HLA-B*40:78', 'HLA-B*40:79',
                            'HLA-B*40:80', 'HLA-B*40:81', 'HLA-B*40:82', 'HLA-B*40:83', 'HLA-B*40:84', 'HLA-B*40:85', 'HLA-B*40:86', 'HLA-B*40:87',
                            'HLA-B*40:88', 'HLA-B*40:89', 'HLA-B*40:90', 'HLA-B*40:91', 'HLA-B*40:92', 'HLA-B*40:93', 'HLA-B*40:94', 'HLA-B*40:95',
                            'HLA-B*40:96', 'HLA-B*40:97', 'HLA-B*40:98', 'HLA-B*40:99', 'HLA-B*40:100', 'HLA-B*40:101', 'HLA-B*40:102', 'HLA-B*40:103',
                            'HLA-B*40:104', 'HLA-B*40:105', 'HLA-B*40:106', 'HLA-B*40:107', 'HLA-B*40:108', 'HLA-B*40:109', 'HLA-B*40:110',
                            'HLA-B*40:111', 'HLA-B*40:112', 'HLA-B*40:113', 'HLA-B*40:114', 'HLA-B*40:115', 'HLA-B*40:116', 'HLA-B*40:117',
                            'HLA-B*40:119', 'HLA-B*40:120', 'HLA-B*40:121', 'HLA-B*40:122', 'HLA-B*40:123', 'HLA-B*40:124', 'HLA-B*40:125',
                            'HLA-B*40:126', 'HLA-B*40:127', 'HLA-B*40:128', 'HLA-B*40:129', 'HLA-B*40:130', 'HLA-B*40:131', 'HLA-B*40:132',
                            'HLA-B*40:134', 'HLA-B*40:135', 'HLA-B*40:136', 'HLA-B*40:137', 'HLA-B*40:138', 'HLA-B*40:139', 'HLA-B*40:140',
                            'HLA-B*40:141', 'HLA-B*40:143', 'HLA-B*40:145', 'HLA-B*40:146', 'HLA-B*40:147', 'HLA-B*41:01', 'HLA-B*41:02', 'HLA-B*41:03',
                            'HLA-B*41:04', 'HLA-B*41:05', 'HLA-B*41:06', 'HLA-B*41:07', 'HLA-B*41:08', 'HLA-B*41:09', 'HLA-B*41:10', 'HLA-B*41:11',
                            'HLA-B*41:12', 'HLA-B*42:01', 'HLA-B*42:02', 'HLA-B*42:04', 'HLA-B*42:05', 'HLA-B*42:06', 'HLA-B*42:07', 'HLA-B*42:08',
                            'HLA-B*42:09', 'HLA-B*42:10', 'HLA-B*42:11', 'HLA-B*42:12', 'HLA-B*42:13', 'HLA-B*42:14', 'HLA-B*44:02', 'HLA-B*44:03',
                            'HLA-B*44:04', 'HLA-B*44:05', 'HLA-B*44:06', 'HLA-B*44:07', 'HLA-B*44:08', 'HLA-B*44:09', 'HLA-B*44:10', 'HLA-B*44:11',
                            'HLA-B*44:12', 'HLA-B*44:13', 'HLA-B*44:14', 'HLA-B*44:15', 'HLA-B*44:16', 'HLA-B*44:17', 'HLA-B*44:18', 'HLA-B*44:20',
                            'HLA-B*44:21', 'HLA-B*44:22', 'HLA-B*44:24', 'HLA-B*44:25', 'HLA-B*44:26', 'HLA-B*44:27', 'HLA-B*44:28', 'HLA-B*44:29',
                            'HLA-B*44:30', 'HLA-B*44:31', 'HLA-B*44:32', 'HLA-B*44:33', 'HLA-B*44:34', 'HLA-B*44:35', 'HLA-B*44:36', 'HLA-B*44:37',
                            'HLA-B*44:38', 'HLA-B*44:39', 'HLA-B*44:40', 'HLA-B*44:41', 'HLA-B*44:42', 'HLA-B*44:43', 'HLA-B*44:44', 'HLA-B*44:45',
                            'HLA-B*44:46', 'HLA-B*44:47', 'HLA-B*44:48', 'HLA-B*44:49', 'HLA-B*44:50', 'HLA-B*44:51', 'HLA-B*44:53', 'HLA-B*44:54',
                            'HLA-B*44:55', 'HLA-B*44:57', 'HLA-B*44:59', 'HLA-B*44:60', 'HLA-B*44:62', 'HLA-B*44:63', 'HLA-B*44:64', 'HLA-B*44:65',
                            'HLA-B*44:66', 'HLA-B*44:67', 'HLA-B*44:68', 'HLA-B*44:69', 'HLA-B*44:70', 'HLA-B*44:71', 'HLA-B*44:72', 'HLA-B*44:73',
                            'HLA-B*44:74', 'HLA-B*44:75', 'HLA-B*44:76', 'HLA-B*44:77', 'HLA-B*44:78', 'HLA-B*44:79', 'HLA-B*44:80', 'HLA-B*44:81',
                            'HLA-B*44:82', 'HLA-B*44:83', 'HLA-B*44:84', 'HLA-B*44:85', 'HLA-B*44:86', 'HLA-B*44:87', 'HLA-B*44:88', 'HLA-B*44:89',
                            'HLA-B*44:90', 'HLA-B*44:91', 'HLA-B*44:92', 'HLA-B*44:93', 'HLA-B*44:94', 'HLA-B*44:95', 'HLA-B*44:96', 'HLA-B*44:97',
                            'HLA-B*44:98', 'HLA-B*44:99', 'HLA-B*44:100', 'HLA-B*44:101', 'HLA-B*44:102', 'HLA-B*44:103', 'HLA-B*44:104',
                            'HLA-B*44:105', 'HLA-B*44:106', 'HLA-B*44:107', 'HLA-B*44:109', 'HLA-B*44:110', 'HLA-B*45:01', 'HLA-B*45:02', 'HLA-B*45:03',
                            'HLA-B*45:04', 'HLA-B*45:05', 'HLA-B*45:06', 'HLA-B*45:07', 'HLA-B*45:08', 'HLA-B*45:09', 'HLA-B*45:10', 'HLA-B*45:11',
                            'HLA-B*45:12', 'HLA-B*46:01', 'HLA-B*46:02', 'HLA-B*46:03', 'HLA-B*46:04', 'HLA-B*46:05', 'HLA-B*46:06', 'HLA-B*46:08',
                            'HLA-B*46:09', 'HLA-B*46:10', 'HLA-B*46:11', 'HLA-B*46:12', 'HLA-B*46:13', 'HLA-B*46:14', 'HLA-B*46:16', 'HLA-B*46:17',
                            'HLA-B*46:18', 'HLA-B*46:19', 'HLA-B*46:20', 'HLA-B*46:21', 'HLA-B*46:22', 'HLA-B*46:23', 'HLA-B*46:24', 'HLA-B*47:01',
                            'HLA-B*47:02', 'HLA-B*47:03', 'HLA-B*47:04', 'HLA-B*47:05', 'HLA-B*47:06', 'HLA-B*47:07', 'HLA-B*48:01', 'HLA-B*48:02',
                            'HLA-B*48:03', 'HLA-B*48:04', 'HLA-B*48:05', 'HLA-B*48:06', 'HLA-B*48:07', 'HLA-B*48:08', 'HLA-B*48:09', 'HLA-B*48:10',
                            'HLA-B*48:11', 'HLA-B*48:12', 'HLA-B*48:13', 'HLA-B*48:14', 'HLA-B*48:15', 'HLA-B*48:16', 'HLA-B*48:17', 'HLA-B*48:18',
                            'HLA-B*48:19', 'HLA-B*48:20', 'HLA-B*48:21', 'HLA-B*48:22', 'HLA-B*48:23', 'HLA-B*49:01', 'HLA-B*49:02', 'HLA-B*49:03',
                            'HLA-B*49:04', 'HLA-B*49:05', 'HLA-B*49:06', 'HLA-B*49:07', 'HLA-B*49:08', 'HLA-B*49:09', 'HLA-B*49:10', 'HLA-B*50:01',
                            'HLA-B*50:02', 'HLA-B*50:04', 'HLA-B*50:05', 'HLA-B*50:06', 'HLA-B*50:07', 'HLA-B*50:08', 'HLA-B*50:09', 'HLA-B*51:01',
                            'HLA-B*51:02', 'HLA-B*51:03', 'HLA-B*51:04', 'HLA-B*51:05', 'HLA-B*51:06', 'HLA-B*51:07', 'HLA-B*51:08', 'HLA-B*51:09',
                            'HLA-B*51:12', 'HLA-B*51:13', 'HLA-B*51:14', 'HLA-B*51:15', 'HLA-B*51:16', 'HLA-B*51:17', 'HLA-B*51:18', 'HLA-B*51:19',
                            'HLA-B*51:20', 'HLA-B*51:21', 'HLA-B*51:22', 'HLA-B*51:23', 'HLA-B*51:24', 'HLA-B*51:26', 'HLA-B*51:28', 'HLA-B*51:29',
                            'HLA-B*51:30', 'HLA-B*51:31', 'HLA-B*51:32', 'HLA-B*51:33', 'HLA-B*51:34', 'HLA-B*51:35', 'HLA-B*51:36', 'HLA-B*51:37',
                            'HLA-B*51:38', 'HLA-B*51:39', 'HLA-B*51:40', 'HLA-B*51:42', 'HLA-B*51:43', 'HLA-B*51:45', 'HLA-B*51:46', 'HLA-B*51:48',
                            'HLA-B*51:49', 'HLA-B*51:50', 'HLA-B*51:51', 'HLA-B*51:52', 'HLA-B*51:53', 'HLA-B*51:54', 'HLA-B*51:55', 'HLA-B*51:56',
                            'HLA-B*51:57', 'HLA-B*51:58', 'HLA-B*51:59', 'HLA-B*51:60', 'HLA-B*51:61', 'HLA-B*51:62', 'HLA-B*51:63', 'HLA-B*51:64',
                            'HLA-B*51:65', 'HLA-B*51:66', 'HLA-B*51:67', 'HLA-B*51:68', 'HLA-B*51:69', 'HLA-B*51:70', 'HLA-B*51:71', 'HLA-B*51:72',
                            'HLA-B*51:73', 'HLA-B*51:74', 'HLA-B*51:75', 'HLA-B*51:76', 'HLA-B*51:77', 'HLA-B*51:78', 'HLA-B*51:79', 'HLA-B*51:80',
                            'HLA-B*51:81', 'HLA-B*51:82', 'HLA-B*51:83', 'HLA-B*51:84', 'HLA-B*51:85', 'HLA-B*51:86', 'HLA-B*51:87', 'HLA-B*51:88',
                            'HLA-B*51:89', 'HLA-B*51:90', 'HLA-B*51:91', 'HLA-B*51:92', 'HLA-B*51:93', 'HLA-B*51:94', 'HLA-B*51:95', 'HLA-B*51:96',
                            'HLA-B*52:01', 'HLA-B*52:02', 'HLA-B*52:03', 'HLA-B*52:04', 'HLA-B*52:05', 'HLA-B*52:06', 'HLA-B*52:07', 'HLA-B*52:08',
                            'HLA-B*52:09', 'HLA-B*52:10', 'HLA-B*52:11', 'HLA-B*52:12', 'HLA-B*52:13', 'HLA-B*52:14', 'HLA-B*52:15', 'HLA-B*52:16',
                            'HLA-B*52:17', 'HLA-B*52:18', 'HLA-B*52:19', 'HLA-B*52:20', 'HLA-B*52:21', 'HLA-B*53:01', 'HLA-B*53:02', 'HLA-B*53:03',
                            'HLA-B*53:04', 'HLA-B*53:05', 'HLA-B*53:06', 'HLA-B*53:07', 'HLA-B*53:08', 'HLA-B*53:09', 'HLA-B*53:10', 'HLA-B*53:11',
                            'HLA-B*53:12', 'HLA-B*53:13', 'HLA-B*53:14', 'HLA-B*53:15', 'HLA-B*53:16', 'HLA-B*53:17', 'HLA-B*53:18', 'HLA-B*53:19',
                            'HLA-B*53:20', 'HLA-B*53:21', 'HLA-B*53:22', 'HLA-B*53:23', 'HLA-B*54:01', 'HLA-B*54:02', 'HLA-B*54:03', 'HLA-B*54:04',
                            'HLA-B*54:06', 'HLA-B*54:07', 'HLA-B*54:09', 'HLA-B*54:10', 'HLA-B*54:11', 'HLA-B*54:12', 'HLA-B*54:13', 'HLA-B*54:14',
                            'HLA-B*54:15', 'HLA-B*54:16', 'HLA-B*54:17', 'HLA-B*54:18', 'HLA-B*54:19', 'HLA-B*54:20', 'HLA-B*54:21', 'HLA-B*54:22',
                            'HLA-B*54:23', 'HLA-B*55:01', 'HLA-B*55:02', 'HLA-B*55:03', 'HLA-B*55:04', 'HLA-B*55:05', 'HLA-B*55:07', 'HLA-B*55:08',
                            'HLA-B*55:09', 'HLA-B*55:10', 'HLA-B*55:11', 'HLA-B*55:12', 'HLA-B*55:13', 'HLA-B*55:14', 'HLA-B*55:15', 'HLA-B*55:16',
                            'HLA-B*55:17', 'HLA-B*55:18', 'HLA-B*55:19', 'HLA-B*55:20', 'HLA-B*55:21', 'HLA-B*55:22', 'HLA-B*55:23', 'HLA-B*55:24',
                            'HLA-B*55:25', 'HLA-B*55:26', 'HLA-B*55:27', 'HLA-B*55:28', 'HLA-B*55:29', 'HLA-B*55:30', 'HLA-B*55:31', 'HLA-B*55:32',
                            'HLA-B*55:33', 'HLA-B*55:34', 'HLA-B*55:35', 'HLA-B*55:36', 'HLA-B*55:37', 'HLA-B*55:38', 'HLA-B*55:39', 'HLA-B*55:40',
                            'HLA-B*55:41', 'HLA-B*55:42', 'HLA-B*55:43', 'HLA-B*56:01', 'HLA-B*56:02', 'HLA-B*56:03', 'HLA-B*56:04', 'HLA-B*56:05',
                            'HLA-B*56:06', 'HLA-B*56:07', 'HLA-B*56:08', 'HLA-B*56:09', 'HLA-B*56:10', 'HLA-B*56:11', 'HLA-B*56:12', 'HLA-B*56:13',
                            'HLA-B*56:14', 'HLA-B*56:15', 'HLA-B*56:16', 'HLA-B*56:17', 'HLA-B*56:18', 'HLA-B*56:20', 'HLA-B*56:21', 'HLA-B*56:22',
                            'HLA-B*56:23', 'HLA-B*56:24', 'HLA-B*56:25', 'HLA-B*56:26', 'HLA-B*56:27', 'HLA-B*56:29', 'HLA-B*57:01', 'HLA-B*57:02',
                            'HLA-B*57:03', 'HLA-B*57:04', 'HLA-B*57:05', 'HLA-B*57:06', 'HLA-B*57:07', 'HLA-B*57:08', 'HLA-B*57:09', 'HLA-B*57:10',
                            'HLA-B*57:11', 'HLA-B*57:12', 'HLA-B*57:13', 'HLA-B*57:14', 'HLA-B*57:15', 'HLA-B*57:16', 'HLA-B*57:17', 'HLA-B*57:18',
                            'HLA-B*57:19', 'HLA-B*57:20', 'HLA-B*57:21', 'HLA-B*57:22', 'HLA-B*57:23', 'HLA-B*57:24', 'HLA-B*57:25', 'HLA-B*57:26',
                            'HLA-B*57:27', 'HLA-B*57:29', 'HLA-B*57:30', 'HLA-B*57:31', 'HLA-B*57:32', 'HLA-B*58:01', 'HLA-B*58:02', 'HLA-B*58:04',
                            'HLA-B*58:05', 'HLA-B*58:06', 'HLA-B*58:07', 'HLA-B*58:08', 'HLA-B*58:09', 'HLA-B*58:11', 'HLA-B*58:12', 'HLA-B*58:13',
                            'HLA-B*58:14', 'HLA-B*58:15', 'HLA-B*58:16', 'HLA-B*58:18', 'HLA-B*58:19', 'HLA-B*58:20', 'HLA-B*58:21', 'HLA-B*58:22',
                            'HLA-B*58:23', 'HLA-B*58:24', 'HLA-B*58:25', 'HLA-B*58:26', 'HLA-B*58:27', 'HLA-B*58:28', 'HLA-B*58:29', 'HLA-B*58:30',
                            'HLA-B*59:01', 'HLA-B*59:02', 'HLA-B*59:03', 'HLA-B*59:04', 'HLA-B*59:05', 'HLA-B*67:01', 'HLA-B*67:02', 'HLA-B*73:01',
                            'HLA-B*73:02', 'HLA-B*78:01', 'HLA-B*78:02', 'HLA-B*78:03', 'HLA-B*78:04', 'HLA-B*78:05', 'HLA-B*78:06', 'HLA-B*78:07',
                            'HLA-B*81:01', 'HLA-B*81:02', 'HLA-B*81:03', 'HLA-B*81:05', 'HLA-B*82:01', 'HLA-B*82:02', 'HLA-B*82:03', 'HLA-B*83:01',
                            'HLA-C*01:02', 'HLA-C*01:03', 'HLA-C*01:04', 'HLA-C*01:05', 'HLA-C*01:06', 'HLA-C*01:07', 'HLA-C*01:08', 'HLA-C*01:09',
                            'HLA-C*01:10', 'HLA-C*01:11', 'HLA-C*01:12', 'HLA-C*01:13', 'HLA-C*01:14', 'HLA-C*01:15', 'HLA-C*01:16', 'HLA-C*01:17',
                            'HLA-C*01:18', 'HLA-C*01:19', 'HLA-C*01:20', 'HLA-C*01:21', 'HLA-C*01:22', 'HLA-C*01:23', 'HLA-C*01:24', 'HLA-C*01:25',
                            'HLA-C*01:26', 'HLA-C*01:27', 'HLA-C*01:28', 'HLA-C*01:29', 'HLA-C*01:30', 'HLA-C*01:31', 'HLA-C*01:32', 'HLA-C*01:33',
                            'HLA-C*01:34', 'HLA-C*01:35', 'HLA-C*01:36', 'HLA-C*01:38', 'HLA-C*01:39', 'HLA-C*01:40', 'HLA-C*02:02', 'HLA-C*02:03',
                            'HLA-C*02:04', 'HLA-C*02:05', 'HLA-C*02:06', 'HLA-C*02:07', 'HLA-C*02:08', 'HLA-C*02:09', 'HLA-C*02:10', 'HLA-C*02:11',
                            'HLA-C*02:12', 'HLA-C*02:13', 'HLA-C*02:14', 'HLA-C*02:15', 'HLA-C*02:16', 'HLA-C*02:17', 'HLA-C*02:18', 'HLA-C*02:19',
                            'HLA-C*02:20', 'HLA-C*02:21', 'HLA-C*02:22', 'HLA-C*02:23', 'HLA-C*02:24', 'HLA-C*02:26', 'HLA-C*02:27', 'HLA-C*02:28',
                            'HLA-C*02:29', 'HLA-C*02:30', 'HLA-C*02:31', 'HLA-C*02:32', 'HLA-C*02:33', 'HLA-C*02:34', 'HLA-C*02:35', 'HLA-C*02:36',
                            'HLA-C*02:37', 'HLA-C*02:39', 'HLA-C*02:40', 'HLA-C*03:01', 'HLA-C*03:02', 'HLA-C*03:03', 'HLA-C*03:04', 'HLA-C*03:05',
                            'HLA-C*03:06', 'HLA-C*03:07', 'HLA-C*03:08', 'HLA-C*03:09', 'HLA-C*03:10', 'HLA-C*03:11', 'HLA-C*03:12', 'HLA-C*03:13',
                            'HLA-C*03:14', 'HLA-C*03:15', 'HLA-C*03:16', 'HLA-C*03:17', 'HLA-C*03:18', 'HLA-C*03:19', 'HLA-C*03:21', 'HLA-C*03:23',
                            'HLA-C*03:24', 'HLA-C*03:25', 'HLA-C*03:26', 'HLA-C*03:27', 'HLA-C*03:28', 'HLA-C*03:29', 'HLA-C*03:30', 'HLA-C*03:31',
                            'HLA-C*03:32', 'HLA-C*03:33', 'HLA-C*03:34', 'HLA-C*03:35', 'HLA-C*03:36', 'HLA-C*03:37', 'HLA-C*03:38', 'HLA-C*03:39',
                            'HLA-C*03:40', 'HLA-C*03:41', 'HLA-C*03:42', 'HLA-C*03:43', 'HLA-C*03:44', 'HLA-C*03:45', 'HLA-C*03:46', 'HLA-C*03:47',
                            'HLA-C*03:48', 'HLA-C*03:49', 'HLA-C*03:50', 'HLA-C*03:51', 'HLA-C*03:52', 'HLA-C*03:53', 'HLA-C*03:54', 'HLA-C*03:55',
                            'HLA-C*03:56', 'HLA-C*03:57', 'HLA-C*03:58', 'HLA-C*03:59', 'HLA-C*03:60', 'HLA-C*03:61', 'HLA-C*03:62', 'HLA-C*03:63',
                            'HLA-C*03:64', 'HLA-C*03:65', 'HLA-C*03:66', 'HLA-C*03:67', 'HLA-C*03:68', 'HLA-C*03:69', 'HLA-C*03:70', 'HLA-C*03:71',
                            'HLA-C*03:72', 'HLA-C*03:73', 'HLA-C*03:74', 'HLA-C*03:75', 'HLA-C*03:76', 'HLA-C*03:77', 'HLA-C*03:78', 'HLA-C*03:79',
                            'HLA-C*03:80', 'HLA-C*03:81', 'HLA-C*03:82', 'HLA-C*03:83', 'HLA-C*03:84', 'HLA-C*03:85', 'HLA-C*03:86', 'HLA-C*03:87',
                            'HLA-C*03:88', 'HLA-C*03:89', 'HLA-C*03:90', 'HLA-C*03:91', 'HLA-C*03:92', 'HLA-C*03:93', 'HLA-C*03:94', 'HLA-C*04:01',
                            'HLA-C*04:03', 'HLA-C*04:04', 'HLA-C*04:05', 'HLA-C*04:06', 'HLA-C*04:07', 'HLA-C*04:08', 'HLA-C*04:10', 'HLA-C*04:11',
                            'HLA-C*04:12', 'HLA-C*04:13', 'HLA-C*04:14', 'HLA-C*04:15', 'HLA-C*04:16', 'HLA-C*04:17', 'HLA-C*04:18', 'HLA-C*04:19',
                            'HLA-C*04:20', 'HLA-C*04:23', 'HLA-C*04:24', 'HLA-C*04:25', 'HLA-C*04:26', 'HLA-C*04:27', 'HLA-C*04:28', 'HLA-C*04:29',
                            'HLA-C*04:30', 'HLA-C*04:31', 'HLA-C*04:32', 'HLA-C*04:33', 'HLA-C*04:34', 'HLA-C*04:35', 'HLA-C*04:36', 'HLA-C*04:37',
                            'HLA-C*04:38', 'HLA-C*04:39', 'HLA-C*04:40', 'HLA-C*04:41', 'HLA-C*04:42', 'HLA-C*04:43', 'HLA-C*04:44', 'HLA-C*04:45',
                            'HLA-C*04:46', 'HLA-C*04:47', 'HLA-C*04:48', 'HLA-C*04:49', 'HLA-C*04:50', 'HLA-C*04:51', 'HLA-C*04:52', 'HLA-C*04:53',
                            'HLA-C*04:54', 'HLA-C*04:55', 'HLA-C*04:56', 'HLA-C*04:57', 'HLA-C*04:58', 'HLA-C*04:60', 'HLA-C*04:61', 'HLA-C*04:62',
                            'HLA-C*04:63', 'HLA-C*04:64', 'HLA-C*04:65', 'HLA-C*04:66', 'HLA-C*04:67', 'HLA-C*04:68', 'HLA-C*04:69', 'HLA-C*04:70',
                            'HLA-C*05:01', 'HLA-C*05:03', 'HLA-C*05:04', 'HLA-C*05:05', 'HLA-C*05:06', 'HLA-C*05:08', 'HLA-C*05:09', 'HLA-C*05:10',
                            'HLA-C*05:11', 'HLA-C*05:12', 'HLA-C*05:13', 'HLA-C*05:14', 'HLA-C*05:15', 'HLA-C*05:16', 'HLA-C*05:17', 'HLA-C*05:18',
                            'HLA-C*05:19', 'HLA-C*05:20', 'HLA-C*05:21', 'HLA-C*05:22', 'HLA-C*05:23', 'HLA-C*05:24', 'HLA-C*05:25', 'HLA-C*05:26',
                            'HLA-C*05:27', 'HLA-C*05:28', 'HLA-C*05:29', 'HLA-C*05:30', 'HLA-C*05:31', 'HLA-C*05:32', 'HLA-C*05:33', 'HLA-C*05:34',
                            'HLA-C*05:35', 'HLA-C*05:36', 'HLA-C*05:37', 'HLA-C*05:38', 'HLA-C*05:39', 'HLA-C*05:40', 'HLA-C*05:41', 'HLA-C*05:42',
                            'HLA-C*05:43', 'HLA-C*05:44', 'HLA-C*05:45', 'HLA-C*06:02', 'HLA-C*06:03', 'HLA-C*06:04', 'HLA-C*06:05', 'HLA-C*06:06',
                            'HLA-C*06:07', 'HLA-C*06:08', 'HLA-C*06:09', 'HLA-C*06:10', 'HLA-C*06:11', 'HLA-C*06:12', 'HLA-C*06:13', 'HLA-C*06:14',
                            'HLA-C*06:15', 'HLA-C*06:17', 'HLA-C*06:18', 'HLA-C*06:19', 'HLA-C*06:20', 'HLA-C*06:21', 'HLA-C*06:22', 'HLA-C*06:23',
                            'HLA-C*06:24', 'HLA-C*06:25', 'HLA-C*06:26', 'HLA-C*06:27', 'HLA-C*06:28', 'HLA-C*06:29', 'HLA-C*06:30', 'HLA-C*06:31',
                            'HLA-C*06:32', 'HLA-C*06:33', 'HLA-C*06:34', 'HLA-C*06:35', 'HLA-C*06:36', 'HLA-C*06:37', 'HLA-C*06:38', 'HLA-C*06:39',
                            'HLA-C*06:40', 'HLA-C*06:41', 'HLA-C*06:42', 'HLA-C*06:43', 'HLA-C*06:44', 'HLA-C*06:45', 'HLA-C*07:01', 'HLA-C*07:02',
                            'HLA-C*07:03', 'HLA-C*07:04', 'HLA-C*07:05', 'HLA-C*07:06', 'HLA-C*07:07', 'HLA-C*07:08', 'HLA-C*07:09', 'HLA-C*07:10',
                            'HLA-C*07:11', 'HLA-C*07:12', 'HLA-C*07:13', 'HLA-C*07:14', 'HLA-C*07:15', 'HLA-C*07:16', 'HLA-C*07:17', 'HLA-C*07:18',
                            'HLA-C*07:19', 'HLA-C*07:20', 'HLA-C*07:21', 'HLA-C*07:22', 'HLA-C*07:23', 'HLA-C*07:24', 'HLA-C*07:25', 'HLA-C*07:26',
                            'HLA-C*07:27', 'HLA-C*07:28', 'HLA-C*07:29', 'HLA-C*07:30', 'HLA-C*07:31', 'HLA-C*07:35', 'HLA-C*07:36', 'HLA-C*07:37',
                            'HLA-C*07:38', 'HLA-C*07:39', 'HLA-C*07:40', 'HLA-C*07:41', 'HLA-C*07:42', 'HLA-C*07:43', 'HLA-C*07:44', 'HLA-C*07:45',
                            'HLA-C*07:46', 'HLA-C*07:47', 'HLA-C*07:48', 'HLA-C*07:49', 'HLA-C*07:50', 'HLA-C*07:51', 'HLA-C*07:52', 'HLA-C*07:53',
                            'HLA-C*07:54', 'HLA-C*07:56', 'HLA-C*07:57', 'HLA-C*07:58', 'HLA-C*07:59', 'HLA-C*07:60', 'HLA-C*07:62', 'HLA-C*07:63',
                            'HLA-C*07:64', 'HLA-C*07:65', 'HLA-C*07:66', 'HLA-C*07:67', 'HLA-C*07:68', 'HLA-C*07:69', 'HLA-C*07:70', 'HLA-C*07:71',
                            'HLA-C*07:72', 'HLA-C*07:73', 'HLA-C*07:74', 'HLA-C*07:75', 'HLA-C*07:76', 'HLA-C*07:77', 'HLA-C*07:78', 'HLA-C*07:79',
                            'HLA-C*07:80', 'HLA-C*07:81', 'HLA-C*07:82', 'HLA-C*07:83', 'HLA-C*07:84', 'HLA-C*07:85', 'HLA-C*07:86', 'HLA-C*07:87',
                            'HLA-C*07:88', 'HLA-C*07:89', 'HLA-C*07:90', 'HLA-C*07:91', 'HLA-C*07:92', 'HLA-C*07:93', 'HLA-C*07:94', 'HLA-C*07:95',
                            'HLA-C*07:96', 'HLA-C*07:97', 'HLA-C*07:99', 'HLA-C*07:100', 'HLA-C*07:101', 'HLA-C*07:102', 'HLA-C*07:103', 'HLA-C*07:105',
                            'HLA-C*07:106', 'HLA-C*07:107', 'HLA-C*07:108', 'HLA-C*07:109', 'HLA-C*07:110', 'HLA-C*07:111', 'HLA-C*07:112',
                            'HLA-C*07:113', 'HLA-C*07:114', 'HLA-C*07:115', 'HLA-C*07:116', 'HLA-C*07:117', 'HLA-C*07:118', 'HLA-C*07:119',
                            'HLA-C*07:120', 'HLA-C*07:122', 'HLA-C*07:123', 'HLA-C*07:124', 'HLA-C*07:125', 'HLA-C*07:126', 'HLA-C*07:127',
                            'HLA-C*07:128', 'HLA-C*07:129', 'HLA-C*07:130', 'HLA-C*07:131', 'HLA-C*07:132', 'HLA-C*07:133', 'HLA-C*07:134',
                            'HLA-C*07:135', 'HLA-C*07:136', 'HLA-C*07:137', 'HLA-C*07:138', 'HLA-C*07:139', 'HLA-C*07:140', 'HLA-C*07:141',
                            'HLA-C*07:142', 'HLA-C*07:143', 'HLA-C*07:144', 'HLA-C*07:145', 'HLA-C*07:146', 'HLA-C*07:147', 'HLA-C*07:148',
                            'HLA-C*07:149', 'HLA-C*08:01', 'HLA-C*08:02', 'HLA-C*08:03', 'HLA-C*08:04', 'HLA-C*08:05', 'HLA-C*08:06', 'HLA-C*08:07',
                            'HLA-C*08:08', 'HLA-C*08:09', 'HLA-C*08:10', 'HLA-C*08:11', 'HLA-C*08:12', 'HLA-C*08:13', 'HLA-C*08:14', 'HLA-C*08:15',
                            'HLA-C*08:16', 'HLA-C*08:17', 'HLA-C*08:18', 'HLA-C*08:19', 'HLA-C*08:20', 'HLA-C*08:21', 'HLA-C*08:22', 'HLA-C*08:23',
                            'HLA-C*08:24', 'HLA-C*08:25', 'HLA-C*08:27', 'HLA-C*08:28', 'HLA-C*08:29', 'HLA-C*08:30', 'HLA-C*08:31', 'HLA-C*08:32',
                            'HLA-C*08:33', 'HLA-C*08:34', 'HLA-C*08:35', 'HLA-C*12:02', 'HLA-C*12:03', 'HLA-C*12:04', 'HLA-C*12:05', 'HLA-C*12:06',
                            'HLA-C*12:07', 'HLA-C*12:08', 'HLA-C*12:09', 'HLA-C*12:10', 'HLA-C*12:11', 'HLA-C*12:12', 'HLA-C*12:13', 'HLA-C*12:14',
                            'HLA-C*12:15', 'HLA-C*12:16', 'HLA-C*12:17', 'HLA-C*12:18', 'HLA-C*12:19', 'HLA-C*12:20', 'HLA-C*12:21', 'HLA-C*12:22',
                            'HLA-C*12:23', 'HLA-C*12:24', 'HLA-C*12:25', 'HLA-C*12:26', 'HLA-C*12:27', 'HLA-C*12:28', 'HLA-C*12:29', 'HLA-C*12:30',
                            'HLA-C*12:31', 'HLA-C*12:32', 'HLA-C*12:33', 'HLA-C*12:34', 'HLA-C*12:35', 'HLA-C*12:36', 'HLA-C*12:37', 'HLA-C*12:38',
                            'HLA-C*12:40', 'HLA-C*12:41', 'HLA-C*12:43', 'HLA-C*12:44', 'HLA-C*14:02', 'HLA-C*14:03', 'HLA-C*14:04', 'HLA-C*14:05',
                            'HLA-C*14:06', 'HLA-C*14:08', 'HLA-C*14:09', 'HLA-C*14:10', 'HLA-C*14:11', 'HLA-C*14:12', 'HLA-C*14:13', 'HLA-C*14:14',
                            'HLA-C*14:15', 'HLA-C*14:16', 'HLA-C*14:17', 'HLA-C*14:18', 'HLA-C*14:19', 'HLA-C*14:20', 'HLA-C*15:02', 'HLA-C*15:03',
                            'HLA-C*15:04', 'HLA-C*15:05', 'HLA-C*15:06', 'HLA-C*15:07', 'HLA-C*15:08', 'HLA-C*15:09', 'HLA-C*15:10', 'HLA-C*15:11',
                            'HLA-C*15:12', 'HLA-C*15:13', 'HLA-C*15:15', 'HLA-C*15:16', 'HLA-C*15:17', 'HLA-C*15:18', 'HLA-C*15:19', 'HLA-C*15:20',
                            'HLA-C*15:21', 'HLA-C*15:22', 'HLA-C*15:23', 'HLA-C*15:24', 'HLA-C*15:25', 'HLA-C*15:26', 'HLA-C*15:27', 'HLA-C*15:28',
                            'HLA-C*15:29', 'HLA-C*15:30', 'HLA-C*15:31', 'HLA-C*15:33', 'HLA-C*15:34', 'HLA-C*15:35', 'HLA-C*16:01', 'HLA-C*16:02',
                            'HLA-C*16:04', 'HLA-C*16:06', 'HLA-C*16:07', 'HLA-C*16:08', 'HLA-C*16:09', 'HLA-C*16:10', 'HLA-C*16:11', 'HLA-C*16:12',
                            'HLA-C*16:13', 'HLA-C*16:14', 'HLA-C*16:15', 'HLA-C*16:17', 'HLA-C*16:18', 'HLA-C*16:19', 'HLA-C*16:20', 'HLA-C*16:21',
                            'HLA-C*16:22', 'HLA-C*16:23', 'HLA-C*16:24', 'HLA-C*16:25', 'HLA-C*16:26', 'HLA-C*17:01', 'HLA-C*17:02', 'HLA-C*17:03',
                            'HLA-C*17:04', 'HLA-C*17:05', 'HLA-C*17:06', 'HLA-C*17:07', 'HLA-C*18:01', 'HLA-C*18:02', 'HLA-C*18:03', 'HLA-G*01:01',
                            'HLA-G*01:02', 'HLA-G*01:03', 'HLA-G*01:04', 'HLA-G*01:06', 'HLA-G*01:07', 'HLA-G*01:08', 'HLA-G*01:09', 'HLA-E*01:01'])

    @property
    def command(self):
        return self.__command

    @property
    def name(self):
        return self.__name

    @property
    def version(self):
        return self.__version

    @property
    def supportedAlleles(self):
        return self.__alleles

    @property
    def supportedLength(self):
        return self.__length

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal allele representation of the predictor
        and returns a string representation

        :param  alleles: The :class:`~epytope.Core.Allele.Allele` for which the
                         internal predictor representation is needed
        :type alleles: list(:class:`~epytope.Core.Allele.Allele`)
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return ["HLA-%s%s:%s" % (a.locus, a.supertype, a.subtype) for a in alleles]

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        result = defaultdict(dict)
        with open(file, "r") as f:
            f = csv.reader(f, delimiter='\t')
            alleles = [x for x in next(f) if x != ""]
            ranks = defaultdict(defaultdict)
            rank_pos = 5
            offset = 3
            header = next(f)
            if "Aff(nM)" in header:  # With option command line option '-ia', which includes prediction score in output file
                scores = defaultdict(defaultdict)
                for row in f:
                    pep_seq = row[PeptideIndex.NETMHCSTABPAN_1_0]
                    for i, a in enumerate(alleles):
                        scores[a][pep_seq] = float(row[ScoreIndex.NETMHCSTABPAN_1_0 + i * Offset.NETMHCSTABPAN_1_0_W_SCORE])
                        ranks[a][pep_seq] = float(row[RankIndex.NETMHCSTABPAN_1_0 + i * Offset.NETMHCSTABPAN_1_0_W_SCORE])
                        # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
                result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}
            else:
                for row in f:
                    pep_seq = row[PeptideIndex.NETMHCSTABPAN_1_0]
                    for i, a in enumerate(alleles):
                        ranks[a][pep_seq] = float(row[RankIndex.NETMHCSTABPAN_1_0 + i * Offset.NETMHCSTABPAN_1_0_WO_SCORE])
                        # Create dictionary with hierarchy: {'Allele1':{'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
                result = {allele: {"Rank":list(ranks.values())[j]} for j, allele in enumerate(alleles)}

        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        # can not be determined netmhcpan does not support --version or similar
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools and writes them to file in the specific format

        NO return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))



class NetMHCII_2_2(AExternalEpitopePrediction):
    """
    Implements a wrapper for NetMHCII

    .. note::

        Nielsen, M., & Lund, O. (2009). NN-align. An artificial neural network-based alignment algorithm for MHC class
        II peptide binding prediction. BMC Bioinformatics, 10(1), 296.

        Nielsen, M., Lundegaard, C., & Lund, O. (2007). Prediction of MHC class II binding affinity using SMM-align,
        a novel stabilization matrix alignment method. BMC Bioinformatics, 8(1), 238.
    """
    __supported_length = frozenset([15])
    __name = "netmhcII"
    __command = 'netMHCII {peptides} -a {alleles} {options} | grep -v "#" > {out}'
    __alleles = frozenset(
        ['HLA-DRB1*01:01', 'HLA-DRB1*03:01', 'HLA-DRB1*04:01', 'HLA-DRB1*04:04', 'HLA-DRB1*04:05', 'HLA-DRB1*07:01', 'HLA-DRB1*08:02', 'HLA-DRB1*09:01',
         'HLA-DRB1*11:01', 'HLA-DRB1*13:02', 'HLA-DRB1*15:01', 'HLA-DRB3*01:01', 'HLA-DRB4*01:01', 'HLA-DRB5*01:01',
         'H-2-Iab', 'H-2-Iad'])
    __version = "2.2"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        for r in f:
            if not r:
                continue
            
            row = r[0].split()
            if not len(row):
                continue
            
            if "HLA" not in row[HLAIndex.NETMHCII_2_2]:
                continue
            allele = row[HLAIndex.NETMHCII_2_2]
            pep = row[PeptideIndex.NETMHCII_2_2]
            scores[allele][pep] = float(row[ScoreIndex.NETMHCII_2_2])

        result = {allele: {"Score":list(scores.values())[j]} for j, allele in enumerate(scores.keys())}

        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools
        and writes them to _file in the specific format

        No return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(">pepe_%i\n%s" % (i, p) for i, p in enumerate(input)))


class NetMHCII_2_3(NetMHCII_2_2):
    """
    Implements a wrapper for NetMHCII 2.3

    .. note::

        Jensen KK, Andreatta M, Marcatili P, Buus S, Greenbaum JA, Yan Z, Sette A, Peters B, and Nielsen M. (2018)
        Improved methods for predicting peptide binding affinity to MHC class II molecules. 
    """
    __supported_length = frozenset([15])
    __name = "netmhcII"
    __command = 'netMHCII {peptides} -a {alleles} {options} | grep -v "#" > {out}'
    __alleles = frozenset(
        ['HLA-DRB1*01:01', 'HLA-DRB1*01:03', 'HLA-DRB1*03:01', 'HLA-DRB1*04:01', 'HLA-DRB1*04:02',
'HLA-DRB1*04:03', 'HLA-DRB1*04:04', 'HLA-DRB1*04:05', 'HLA-DRB1*07:01', 'HLA-DRB1*08:01',
'HLA-DRB1*08:02', 'HLA-DRB1*09:01', 'HLA-DRB1*10:01', 'HLA-DRB1*11:01', 'HLA-DRB1*12:01',
'HLA-DRB1*13:01', 'HLA-DRB1*13:02', 'HLA-DRB1*15:01', 'HLA-DRB1*16:02', 'HLA-DRB3*01:01',
'HLA-DRB3*02:02', 'HLA-DRB3*03:01', 'HLA-DRB4*01:01', 'HLA-DRB4*01:03', 'HLA-DRB5*01:01',
'HLA-DPA1*01:03-DPB1*02:01', 'HLA-DPA1*01:03-DPB1*03:01', 'HLA-DPA1*01:03-DPB1*04:01',
'HLA-DPA1*01:03-DPB1*04:02', 'HLA-DPA1*01:03-DPB1*06:01', 'HLA-DPA1*02:01-DPB1*01:01', 'HLA-DPA1*02:01-DPB1*05:01', 'HLA-DPA1*02:01-DPB1*14:01',
'HLA-DPA1*03:01-DPB1*04:02', 'HLA-DQA1*01:01-DQB1*05:01', 'HLA-DQA1*01:02-DQB1*05:01', 'HLA-DQA1*01:02-DQB1*05:02', 'HLA-DQA1*01:02-DQB1*06:02',
'HLA-DQA1*01:03-DQB1*06:03', 'HLA-DQA1*01:04-DQB1*05:03', 'HLA-DQA1*02:01-DQB1*02:02', 'HLA-DQA1*02:01-DQB1*03:01', 'HLA-DQA1*02:01-DQB1*03:03',
'HLA-DQA1*02:01-DQB1*04:02', 'HLA-DQA1*03:01-DQB1*03:01', 'HLA-DQA1*03:01-DQB1*03:02', 'HLA-DQA1*03:03-DQB1*04:02', 'HLA-DQA1*04:01-DQB1*04:02',
'HLA-DQA1*05:01-DQB1*02:01', 'HLA-DQA1*05:01-DQB1*03:01', 'HLA-DQA1*05:01-DQB1*03:02', 'HLA-DQA1*05:01-DQB1*03:03', 'HLA-DQA1*05:01-DQB1*04:02',
'HLA-DQA1*06:01-DQB1*04:02', 'H-2-Iab', 'H-2-Iad', 'H-2-Iak', 'H-2-Ias', 'H-2-Iau', 'H-2-Iad', 'H-2-Iak'])
    __version = "2.3"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        elif isinstance(allele, CombinedAllele):
            return '%s-%s%s%s-%s%s%s' % (allele.organism, allele.alpha_locus, allele.alpha_supertype, allele.alpha_subtype,
                                          allele.beta_locus, allele.beta_supertype, allele.beta_subtype)
        else:
            return "%s_%s%s" % (allele.locus, allele.supertype, allele.subtype)


    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)

        for r in f:
            if not r:
                continue
            
            row = r[0].split()
            if not len(row):
                continue
            
            if all(prefix not in row[HLAIndex.NETMHCII_2_3] for prefix in ['HLA-', 'H-2', 'D']):
                continue

            allele = row[HLAIndex.NETMHCII_2_3]
            
            pep = row[PeptideIndex.NETMHCII_2_3]
            scores[allele][pep] = float(row[ScoreIndex.NETMHCII_2_3])
            ranks[allele][pep] = float(row[ScoreIndex.NETMHCII_2_3])
            

        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(scores.keys())}

        return result


class NetMHCIIpan_3_0(AExternalEpitopePrediction):
    """
    Implements a wrapper for NetMHCIIpan.

    .. note::

        Andreatta, M., Karosiene, E., Rasmussen, M., Stryhn, A., Buus, S., & Nielsen, M. (2015).
        Accurate pan-specific prediction of peptide-MHC class II binding affinity with improved binding
        core identification. Immunogenetics, 1-10.
    """

    __supported_length = frozenset([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    __name = "netmhcIIpan"
    __command = "netMHCIIpan -f {peptides} -inptype 1 -a {alleles} {options} -xls -xlsfile {out}"
    __alleles = frozenset(
        ['HLA-DRB1*01:01', 'HLA-DRB1*01:02', 'HLA-DRB1*01:03', 'HLA-DRB1*01:04', 'HLA-DRB1*01:05', 'HLA-DRB1*01:06',
         'HLA-DRB1*01:07', 'HLA-DRB1*01:08', 'HLA-DRB1*01:09', 'HLA-DRB1*01:10', 'HLA-DRB1*01:11', 'HLA-DRB1*01:12',
         'HLA-DRB1*01:13', 'HLA-DRB1*01:14', 'HLA-DRB1*01:15', 'HLA-DRB1*01:16', 'HLA-DRB1*01:17', 'HLA-DRB1*01:18',
         'HLA-DRB1*01:19', 'HLA-DRB1*01:20', 'HLA-DRB1*01:21', 'HLA-DRB1*01:22', 'HLA-DRB1*01:23', 'HLA-DRB1*01:24',
         'HLA-DRB1*01:25', 'HLA-DRB1*01:26', 'HLA-DRB1*01:27', 'HLA-DRB1*01:28', 'HLA-DRB1*01:29', 'HLA-DRB1*01:30',
         'HLA-DRB1*01:31', 'HLA-DRB1*01:32', 'HLA-DRB1*03:01', 'HLA-DRB1*03:02', 'HLA-DRB1*03:03', 'HLA-DRB1*03:04',
         'HLA-DRB1*03:05', 'HLA-DRB1*03:06', 'HLA-DRB1*03:07', 'HLA-DRB1*03:08', 'HLA-DRB1*03:10', 'HLA-DRB1*03:11',
         'HLA-DRB1*03:13', 'HLA-DRB1*03:14', 'HLA-DRB1*03:15', 'HLA-DRB1*03:17', 'HLA-DRB1*03:18', 'HLA-DRB1*03:19',
         'HLA-DRB1*03:20', 'HLA-DRB1*03:21', 'HLA-DRB1*03:22', 'HLA-DRB1*03:23', 'HLA-DRB1*03:24', 'HLA-DRB1*03:25',
         'HLA-DRB1*03:26', 'HLA-DRB1*03:27', 'HLA-DRB1*03:28', 'HLA-DRB1*03:29', 'HLA-DRB1*03:30', 'HLA-DRB1*03:31',
         'HLA-DRB1*03:32', 'HLA-DRB1*03:33', 'HLA-DRB1*03:34', 'HLA-DRB1*03:35', 'HLA-DRB1*03:36', 'HLA-DRB1*03:37',
         'HLA-DRB1*03:38', 'HLA-DRB1*03:39', 'HLA-DRB1*03:40', 'HLA-DRB1*03:41', 'HLA-DRB1*03:42', 'HLA-DRB1*03:43',
         'HLA-DRB1*03:44', 'HLA-DRB1*03:45', 'HLA-DRB1*03:46', 'HLA-DRB1*03:47', 'HLA-DRB1*03:48', 'HLA-DRB1*03:49',
         'HLA-DRB1*03:50', 'HLA-DRB1*03:51', 'HLA-DRB1*03:52', 'HLA-DRB1*03:53', 'HLA-DRB1*03:54', 'HLA-DRB1*03:55',
         'HLA-DRB1*04:01', 'HLA-DRB1*04:02', 'HLA-DRB1*04:03', 'HLA-DRB1*04:04', 'HLA-DRB1*04:05', 'HLA-DRB1*04:06',
         'HLA-DRB1*04:07', 'HLA-DRB1*04:08', 'HLA-DRB1*04:09', 'HLA-DRB1*04:10', 'HLA-DRB1*04:11', 'HLA-DRB1*04:12',
         'HLA-DRB1*04:13', 'HLA-DRB1*04:14', 'HLA-DRB1*04:15', 'HLA-DRB1*04:16', 'HLA-DRB1*04:17', 'HLA-DRB1*04:18',
         'HLA-DRB1*04:19', 'HLA-DRB1*04:21', 'HLA-DRB1*04:22', 'HLA-DRB1*04:23', 'HLA-DRB1*04:24', 'HLA-DRB1*04:26',
         'HLA-DRB1*04:27', 'HLA-DRB1*04:28', 'HLA-DRB1*04:29', 'HLA-DRB1*04:30', 'HLA-DRB1*04:31', 'HLA-DRB1*04:33',
         'HLA-DRB1*04:34', 'HLA-DRB1*04:35', 'HLA-DRB1*04:36', 'HLA-DRB1*04:37', 'HLA-DRB1*04:38', 'HLA-DRB1*04:39',
         'HLA-DRB1*04:40', 'HLA-DRB1*04:41', 'HLA-DRB1*04:42', 'HLA-DRB1*04:43', 'HLA-DRB1*04:44', 'HLA-DRB1*04:45',
         'HLA-DRB1*04:46', 'HLA-DRB1*04:47', 'HLA-DRB1*04:48', 'HLA-DRB1*04:49', 'HLA-DRB1*04:50', 'HLA-DRB1*04:51',
         'HLA-DRB1*04:52', 'HLA-DRB1*04:53', 'HLA-DRB1*04:54', 'HLA-DRB1*04:55', 'HLA-DRB1*04:56', 'HLA-DRB1*04:57',
         'HLA-DRB1*04:58', 'HLA-DRB1*04:59', 'HLA-DRB1*04:60', 'HLA-DRB1*04:61', 'HLA-DRB1*04:62', 'HLA-DRB1*04:63',
         'HLA-DRB1*04:64', 'HLA-DRB1*04:65', 'HLA-DRB1*04:66', 'HLA-DRB1*04:67', 'HLA-DRB1*04:68', 'HLA-DRB1*04:69',
         'HLA-DRB1*04:70', 'HLA-DRB1*04:71', 'HLA-DRB1*04:72', 'HLA-DRB1*04:73', 'HLA-DRB1*04:74', 'HLA-DRB1*04:75',
         'HLA-DRB1*04:76', 'HLA-DRB1*04:77', 'HLA-DRB1*04:78', 'HLA-DRB1*04:79', 'HLA-DRB1*04:80', 'HLA-DRB1*04:82',
         'HLA-DRB1*04:83', 'HLA-DRB1*04:84', 'HLA-DRB1*04:85', 'HLA-DRB1*04:86', 'HLA-DRB1*04:87', 'HLA-DRB1*04:88',
         'HLA-DRB1*04:89', 'HLA-DRB1*04:91', 'HLA-DRB1*07:01', 'HLA-DRB1*07:03', 'HLA-DRB1*07:04', 'HLA-DRB1*07:05',
         'HLA-DRB1*07:06', 'HLA-DRB1*07:07', 'HLA-DRB1*07:08', 'HLA-DRB1*07:09', 'HLA-DRB1*07:11', 'HLA-DRB1*07:12',
         'HLA-DRB1*07:13', 'HLA-DRB1*07:14', 'HLA-DRB1*07:15', 'HLA-DRB1*07:16', 'HLA-DRB1*07:17', 'HLA-DRB1*07:19',
         'HLA-DRB1*08:01', 'HLA-DRB1*08:02', 'HLA-DRB1*08:03', 'HLA-DRB1*08:04', 'HLA-DRB1*08:05', 'HLA-DRB1*08:06',
         'HLA-DRB1*08:07', 'HLA-DRB1*08:08', 'HLA-DRB1*08:09', 'HLA-DRB1*08:10', 'HLA-DRB1*08:11', 'HLA-DRB1*08:12',
         'HLA-DRB1*08:13', 'HLA-DRB1*08:14', 'HLA-DRB1*08:15', 'HLA-DRB1*08:16', 'HLA-DRB1*08:18', 'HLA-DRB1*08:19',
         'HLA-DRB1*08:20', 'HLA-DRB1*08:21', 'HLA-DRB1*08:22', 'HLA-DRB1*08:23', 'HLA-DRB1*08:24', 'HLA-DRB1*08:25',
         'HLA-DRB1*08:26', 'HLA-DRB1*08:27', 'HLA-DRB1*08:28', 'HLA-DRB1*08:29', 'HLA-DRB1*08:30', 'HLA-DRB1*08:31',
         'HLA-DRB1*08:32', 'HLA-DRB1*08:33', 'HLA-DRB1*08:34', 'HLA-DRB1*08:35', 'HLA-DRB1*08:36', 'HLA-DRB1*08:37',
         'HLA-DRB1*08:38', 'HLA-DRB1*08:39', 'HLA-DRB1*08:40', 'HLA-DRB1*09:01', 'HLA-DRB1*09:02', 'HLA-DRB1*09:03',
         'HLA-DRB1*09:04', 'HLA-DRB1*09:05', 'HLA-DRB1*09:06', 'HLA-DRB1*09:07', 'HLA-DRB1*09:08', 'HLA-DRB1*09:09',
         'HLA-DRB1*10:01', 'HLA-DRB1*10:02', 'HLA-DRB1*10:03', 'HLA-DRB1*11:01', 'HLA-DRB1*11:02', 'HLA-DRB1*11:03',
         'HLA-DRB1*11:04', 'HLA-DRB1*11:05', 'HLA-DRB1*11:06', 'HLA-DRB1*11:07', 'HLA-DRB1*11:08', 'HLA-DRB1*11:09',
         'HLA-DRB1*11:10', 'HLA-DRB1*11:11', 'HLA-DRB1*11:12', 'HLA-DRB1*11:13', 'HLA-DRB1*11:14', 'HLA-DRB1*11:15',
         'HLA-DRB1*11:16', 'HLA-DRB1*11:17', 'HLA-DRB1*11:18', 'HLA-DRB1*11:19', 'HLA-DRB1*11:20', 'HLA-DRB1*11:21',
         'HLA-DRB1*11:24', 'HLA-DRB1*11:25', 'HLA-DRB1*11:27', 'HLA-DRB1*11:28', 'HLA-DRB1*11:29', 'HLA-DRB1*11:30',
         'HLA-DRB1*11:31', 'HLA-DRB1*11:32', 'HLA-DRB1*11:33', 'HLA-DRB1*11:34', 'HLA-DRB1*11:35', 'HLA-DRB1*11:36',
         'HLA-DRB1*11:37', 'HLA-DRB1*11:38', 'HLA-DRB1*11:39', 'HLA-DRB1*11:41', 'HLA-DRB1*11:42', 'HLA-DRB1*11:43',
         'HLA-DRB1*11:44', 'HLA-DRB1*11:45', 'HLA-DRB1*11:46', 'HLA-DRB1*11:47', 'HLA-DRB1*11:48', 'HLA-DRB1*11:49',
         'HLA-DRB1*11:50', 'HLA-DRB1*11:51', 'HLA-DRB1*11:52', 'HLA-DRB1*11:53', 'HLA-DRB1*11:54', 'HLA-DRB1*11:55',
         'HLA-DRB1*11:56', 'HLA-DRB1*11:57', 'HLA-DRB1*11:58', 'HLA-DRB1*11:59', 'HLA-DRB1*11:60', 'HLA-DRB1*11:61',
         'HLA-DRB1*11:62', 'HLA-DRB1*11:63', 'HLA-DRB1*11:64', 'HLA-DRB1*11:65', 'HLA-DRB1*11:66', 'HLA-DRB1*11:67',
         'HLA-DRB1*11:68', 'HLA-DRB1*11:69', 'HLA-DRB1*11:70', 'HLA-DRB1*11:72', 'HLA-DRB1*11:73', 'HLA-DRB1*11:74',
         'HLA-DRB1*11:75', 'HLA-DRB1*11:76', 'HLA-DRB1*11:77', 'HLA-DRB1*11:78', 'HLA-DRB1*11:79', 'HLA-DRB1*11:80',
         'HLA-DRB1*11:81', 'HLA-DRB1*11:82', 'HLA-DRB1*11:83', 'HLA-DRB1*11:84', 'HLA-DRB1*11:85', 'HLA-DRB1*11:86',
         'HLA-DRB1*11:87', 'HLA-DRB1*11:88', 'HLA-DRB1*11:89', 'HLA-DRB1*11:90', 'HLA-DRB1*11:91', 'HLA-DRB1*11:92',
         'HLA-DRB1*11:93', 'HLA-DRB1*11:94', 'HLA-DRB1*11:95', 'HLA-DRB1*11:96', 'HLA-DRB1*12:01', 'HLA-DRB1*12:02',
         'HLA-DRB1*12:03', 'HLA-DRB1*12:04', 'HLA-DRB1*12:05', 'HLA-DRB1*12:06', 'HLA-DRB1*12:07', 'HLA-DRB1*12:08',
         'HLA-DRB1*12:09', 'HLA-DRB1*12:10', 'HLA-DRB1*12:11', 'HLA-DRB1*12:12', 'HLA-DRB1*12:13', 'HLA-DRB1*12:14',
         'HLA-DRB1*12:15', 'HLA-DRB1*12:16', 'HLA-DRB1*12:17', 'HLA-DRB1*12:18', 'HLA-DRB1*12:19', 'HLA-DRB1*12:20',
         'HLA-DRB1*12:21', 'HLA-DRB1*12:22', 'HLA-DRB1*12:23', 'HLA-DRB1*13:01', 'HLA-DRB1*13:02', 'HLA-DRB1*13:03',
         'HLA-DRB1*13:04', 'HLA-DRB1*13:05', 'HLA-DRB1*13:06', 'HLA-DRB1*13:07', 'HLA-DRB1*13:08', 'HLA-DRB1*13:09',
         'HLA-DRB1*13:10', 'HLA-DRB1*13:100', 'HLA-DRB1*13:101', 'HLA-DRB1*13:11', 'HLA-DRB1*13:12', 'HLA-DRB1*13:13',
         'HLA-DRB1*13:14', 'HLA-DRB1*13:15', 'HLA-DRB1*13:16', 'HLA-DRB1*13:17', 'HLA-DRB1*13:18', 'HLA-DRB1*13:19',
         'HLA-DRB1*13:20', 'HLA-DRB1*13:21', 'HLA-DRB1*13:22', 'HLA-DRB1*13:23', 'HLA-DRB1*13:24', 'HLA-DRB1*13:26',
         'HLA-DRB1*13:27', 'HLA-DRB1*13:29', 'HLA-DRB1*13:30', 'HLA-DRB1*13:31', 'HLA-DRB1*13:32', 'HLA-DRB1*13:33',
         'HLA-DRB1*13:34', 'HLA-DRB1*13:35', 'HLA-DRB1*13:36', 'HLA-DRB1*13:37', 'HLA-DRB1*13:38', 'HLA-DRB1*13:39',
         'HLA-DRB1*13:41', 'HLA-DRB1*13:42', 'HLA-DRB1*13:43', 'HLA-DRB1*13:44', 'HLA-DRB1*13:46', 'HLA-DRB1*13:47',
         'HLA-DRB1*13:48', 'HLA-DRB1*13:49', 'HLA-DRB1*13:50', 'HLA-DRB1*13:51', 'HLA-DRB1*13:52', 'HLA-DRB1*13:53',
         'HLA-DRB1*13:54', 'HLA-DRB1*13:55', 'HLA-DRB1*13:56', 'HLA-DRB1*13:57', 'HLA-DRB1*13:58', 'HLA-DRB1*13:59',
         'HLA-DRB1*13:60', 'HLA-DRB1*13:61', 'HLA-DRB1*13:62', 'HLA-DRB1*13:63', 'HLA-DRB1*13:64', 'HLA-DRB1*13:65',
         'HLA-DRB1*13:66', 'HLA-DRB1*13:67', 'HLA-DRB1*13:68', 'HLA-DRB1*13:69', 'HLA-DRB1*13:70', 'HLA-DRB1*13:71',
         'HLA-DRB1*13:72', 'HLA-DRB1*13:73', 'HLA-DRB1*13:74', 'HLA-DRB1*13:75', 'HLA-DRB1*13:76', 'HLA-DRB1*13:77',
         'HLA-DRB1*13:78', 'HLA-DRB1*13:79', 'HLA-DRB1*13:80', 'HLA-DRB1*13:81', 'HLA-DRB1*13:82', 'HLA-DRB1*13:83',
         'HLA-DRB1*13:84', 'HLA-DRB1*13:85', 'HLA-DRB1*13:86', 'HLA-DRB1*13:87', 'HLA-DRB1*13:88', 'HLA-DRB1*13:89',
         'HLA-DRB1*13:90', 'HLA-DRB1*13:91', 'HLA-DRB1*13:92', 'HLA-DRB1*13:93', 'HLA-DRB1*13:94', 'HLA-DRB1*13:95',
         'HLA-DRB1*13:96', 'HLA-DRB1*13:97', 'HLA-DRB1*13:98', 'HLA-DRB1*13:99', 'HLA-DRB1*14:01', 'HLA-DRB1*14:02',
         'HLA-DRB1*14:03', 'HLA-DRB1*14:04', 'HLA-DRB1*14:05', 'HLA-DRB1*14:06', 'HLA-DRB1*14:07', 'HLA-DRB1*14:08',
         'HLA-DRB1*14:09', 'HLA-DRB1*14:10', 'HLA-DRB1*14:11', 'HLA-DRB1*14:12', 'HLA-DRB1*14:13', 'HLA-DRB1*14:14',
         'HLA-DRB1*14:15', 'HLA-DRB1*14:16', 'HLA-DRB1*14:17', 'HLA-DRB1*14:18', 'HLA-DRB1*14:19', 'HLA-DRB1*14:20',
         'HLA-DRB1*14:21', 'HLA-DRB1*14:22', 'HLA-DRB1*14:23', 'HLA-DRB1*14:24', 'HLA-DRB1*14:25', 'HLA-DRB1*14:26',
         'HLA-DRB1*14:27', 'HLA-DRB1*14:28', 'HLA-DRB1*14:29', 'HLA-DRB1*14:30', 'HLA-DRB1*14:31', 'HLA-DRB1*14:32',
         'HLA-DRB1*14:33', 'HLA-DRB1*14:34', 'HLA-DRB1*14:35', 'HLA-DRB1*14:36', 'HLA-DRB1*14:37', 'HLA-DRB1*14:38',
         'HLA-DRB1*14:39', 'HLA-DRB1*14:40', 'HLA-DRB1*14:41', 'HLA-DRB1*14:42', 'HLA-DRB1*14:43', 'HLA-DRB1*14:44',
         'HLA-DRB1*14:45', 'HLA-DRB1*14:46', 'HLA-DRB1*14:47', 'HLA-DRB1*14:48', 'HLA-DRB1*14:49', 'HLA-DRB1*14:50',
         'HLA-DRB1*14:51', 'HLA-DRB1*14:52', 'HLA-DRB1*14:53', 'HLA-DRB1*14:54', 'HLA-DRB1*14:55', 'HLA-DRB1*14:56',
         'HLA-DRB1*14:57', 'HLA-DRB1*14:58', 'HLA-DRB1*14:59', 'HLA-DRB1*14:60', 'HLA-DRB1*14:61', 'HLA-DRB1*14:62',
         'HLA-DRB1*14:63', 'HLA-DRB1*14:64', 'HLA-DRB1*14:65', 'HLA-DRB1*14:67', 'HLA-DRB1*14:68', 'HLA-DRB1*14:69',
         'HLA-DRB1*14:70', 'HLA-DRB1*14:71', 'HLA-DRB1*14:72', 'HLA-DRB1*14:73', 'HLA-DRB1*14:74', 'HLA-DRB1*14:75',
         'HLA-DRB1*14:76', 'HLA-DRB1*14:77', 'HLA-DRB1*14:78', 'HLA-DRB1*14:79', 'HLA-DRB1*14:80', 'HLA-DRB1*14:81',
         'HLA-DRB1*14:82', 'HLA-DRB1*14:83', 'HLA-DRB1*14:84', 'HLA-DRB1*14:85', 'HLA-DRB1*14:86', 'HLA-DRB1*14:87',
         'HLA-DRB1*14:88', 'HLA-DRB1*14:89', 'HLA-DRB1*14:90', 'HLA-DRB1*14:91', 'HLA-DRB1*14:93', 'HLA-DRB1*14:94',
         'HLA-DRB1*14:95', 'HLA-DRB1*14:96', 'HLA-DRB1*14:97', 'HLA-DRB1*14:98', 'HLA-DRB1*14:99', 'HLA-DRB1*15:01',
         'HLA-DRB1*15:02', 'HLA-DRB1*15:03', 'HLA-DRB1*15:04', 'HLA-DRB1*15:05', 'HLA-DRB1*15:06', 'HLA-DRB1*15:07',
         'HLA-DRB1*15:08', 'HLA-DRB1*15:09', 'HLA-DRB1*15:10', 'HLA-DRB1*15:11', 'HLA-DRB1*15:12', 'HLA-DRB1*15:13',
         'HLA-DRB1*15:14', 'HLA-DRB1*15:15', 'HLA-DRB1*15:16', 'HLA-DRB1*15:18', 'HLA-DRB1*15:19', 'HLA-DRB1*15:20',
         'HLA-DRB1*15:21', 'HLA-DRB1*15:22', 'HLA-DRB1*15:23', 'HLA-DRB1*15:24', 'HLA-DRB1*15:25', 'HLA-DRB1*15:26',
         'HLA-DRB1*15:27', 'HLA-DRB1*15:28', 'HLA-DRB1*15:29', 'HLA-DRB1*15:30', 'HLA-DRB1*15:31', 'HLA-DRB1*15:32',
         'HLA-DRB1*15:33', 'HLA-DRB1*15:34', 'HLA-DRB1*15:35', 'HLA-DRB1*15:36', 'HLA-DRB1*15:37', 'HLA-DRB1*15:38',
         'HLA-DRB1*15:39', 'HLA-DRB1*15:40', 'HLA-DRB1*15:41', 'HLA-DRB1*15:42', 'HLA-DRB1*15:43', 'HLA-DRB1*15:44',
         'HLA-DRB1*15:45', 'HLA-DRB1*15:46', 'HLA-DRB1*15:47', 'HLA-DRB1*15:48', 'HLA-DRB1*15:49', 'HLA-DRB1*16:01',
         'HLA-DRB1*16:02', 'HLA-DRB1*16:03', 'HLA-DRB1*16:04', 'HLA-DRB1*16:05', 'HLA-DRB1*16:07', 'HLA-DRB1*16:08',
         'HLA-DRB1*16:09', 'HLA-DRB1*16:10', 'HLA-DRB1*16:11', 'HLA-DRB1*16:12', 'HLA-DRB1*16:14', 'HLA-DRB1*16:15',
         'HLA-DRB1*16:16', 'HLA-DRB3*01:01', 'HLA-DRB3*01:04', 'HLA-DRB3*01:05', 'HLA-DRB3*01:08', 'HLA-DRB3*01:09',
         'HLA-DRB3*01:11', 'HLA-DRB3*01:12', 'HLA-DRB3*01:13', 'HLA-DRB3*01:14', 'HLA-DRB3*02:01', 'HLA-DRB3*02:02',
         'HLA-DRB3*02:04', 'HLA-DRB3*02:05', 'HLA-DRB3*02:09', 'HLA-DRB3*02:10', 'HLA-DRB3*02:11', 'HLA-DRB3*02:12',
         'HLA-DRB3*02:13', 'HLA-DRB3*02:14', 'HLA-DRB3*02:15', 'HLA-DRB3*02:16', 'HLA-DRB3*02:17', 'HLA-DRB3*02:18',
         'HLA-DRB3*02:19', 'HLA-DRB3*02:20', 'HLA-DRB3*02:21', 'HLA-DRB3*02:22', 'HLA-DRB3*02:23', 'HLA-DRB3*02:24',
         'HLA-DRB3*02:25', 'HLA-DRB3*03:01', 'HLA-DRB3*03:03', 'HLA-DRB4*01:01', 'HLA-DRB4*01:03', 'HLA-DRB4*01:04',
         'HLA-DRB4*01:06', 'HLA-DRB4*01:07', 'HLA-DRB4*01:08', 'HLA-DRB5*01:01', 'HLA-DRB5*01:02', 'HLA-DRB5*01:03',
         'HLA-DRB5*01:04', 'HLA-DRB5*01:05', 'HLA-DRB5*01:06', 'HLA-DRB5*01:08N', 'HLA-DRB5*01:11', 'HLA-DRB5*01:12',
         'HLA-DRB5*01:13', 'HLA-DRB5*01:14', 'HLA-DRB5*02:02', 'HLA-DRB5*02:03', 'HLA-DRB5*02:04', 'HLA-DRB5*02:05',
         'HLA-DPA1*01:03-DPB1*01:01', 'HLA-DPA1*01:03-DPB1*02:01', 'HLA-DPA1*01:03-DPB1*02:02', 'HLA-DPA1*01:03-DPB1*03:01',
         'HLA-DPA1*01:03-DPB1*04:01', 'HLA-DPA1*01:03-DPB1*04:02', 'HLA-DPA1*01:03-DPB1*05:01', 'HLA-DPA1*01:03-DPB1*06:01',
         'HLA-DPA1*01:03-DPB1*08:01', 'HLA-DPA1*01:03-DPB1*09:01', 'HLA-DPA1*01:03-DPB1*10:001', 'HLA-DPA1*01:03-DPB1*10:01',
         'HLA-DPA1*01:03-DPB1*10:101', 'HLA-DPA1*01:03-DPB1*10:201',
         'HLA-DPA1*01:03-DPB1*10:301', 'HLA-DPA1*01:03-DPB1*10:401',
         'HLA-DPA1*01:03-DPB1*10:501', 'HLA-DPA1*01:03-DPB1*10:601', 'HLA-DPA1*01:03-DPB1*10:701', 'HLA-DPA1*01:03-DPB1*10:801',
         'HLA-DPA1*01:03-DPB1*10:901', 'HLA-DPA1*01:03-DPB1*11:001',
         'HLA-DPA1*01:03-DPB1*11:01', 'HLA-DPA1*01:03-DPB1*11:101', 'HLA-DPA1*01:03-DPB1*11:201', 'HLA-DPA1*01:03-DPB1*11:301',
         'HLA-DPA1*01:03-DPB1*11:401', 'HLA-DPA1*01:03-DPB1*11:501',
         'HLA-DPA1*01:03-DPB1*11:601', 'HLA-DPA1*01:03-DPB1*11:701', 'HLA-DPA1*01:03-DPB1*11:801', 'HLA-DPA1*01:03-DPB1*11:901',
         'HLA-DPA1*01:03-DPB1*12:101', 'HLA-DPA1*01:03-DPB1*12:201',
         'HLA-DPA1*01:03-DPB1*12:301', 'HLA-DPA1*01:03-DPB1*12:401', 'HLA-DPA1*01:03-DPB1*12:501', 'HLA-DPA1*01:03-DPB1*12:601',
         'HLA-DPA1*01:03-DPB1*12:701', 'HLA-DPA1*01:03-DPB1*12:801',
         'HLA-DPA1*01:03-DPB1*12:901', 'HLA-DPA1*01:03-DPB1*13:001', 'HLA-DPA1*01:03-DPB1*13:01', 'HLA-DPA1*01:03-DPB1*13:101',
         'HLA-DPA1*01:03-DPB1*13:201', 'HLA-DPA1*01:03-DPB1*13:301',
         'HLA-DPA1*01:03-DPB1*13:401', 'HLA-DPA1*01:03-DPB1*14:01', 'HLA-DPA1*01:03-DPB1*15:01', 'HLA-DPA1*01:03-DPB1*16:01',
         'HLA-DPA1*01:03-DPB1*17:01', 'HLA-DPA1*01:03-DPB1*18:01',
         'HLA-DPA1*01:03-DPB1*19:01', 'HLA-DPA1*01:03-DPB1*20:01', 'HLA-DPA1*01:03-DPB1*21:01', 'HLA-DPA1*01:03-DPB1*22:01',
         'HLA-DPA1*01:03-DPB1*23:01', 'HLA-DPA1*01:03-DPB1*24:01',
         'HLA-DPA1*01:03-DPB1*25:01', 'HLA-DPA1*01:03-DPB1*26:01', 'HLA-DPA1*01:03-DPB1*27:01', 'HLA-DPA1*01:03-DPB1*28:01',
         'HLA-DPA1*01:03-DPB1*29:01', 'HLA-DPA1*01:03-DPB1*30:01',
         'HLA-DPA1*01:03-DPB1*31:01', 'HLA-DPA1*01:03-DPB1*32:01', 'HLA-DPA1*01:03-DPB1*33:01', 'HLA-DPA1*01:03-DPB1*34:01',
         'HLA-DPA1*01:03-DPB1*35:01', 'HLA-DPA1*01:03-DPB1*36:01',
         'HLA-DPA1*01:03-DPB1*37:01', 'HLA-DPA1*01:03-DPB1*38:01', 'HLA-DPA1*01:03-DPB1*39:01', 'HLA-DPA1*01:03-DPB1*40:01',
         'HLA-DPA1*01:03-DPB1*41:01', 'HLA-DPA1*01:03-DPB1*44:01',
         'HLA-DPA1*01:03-DPB1*45:01', 'HLA-DPA1*01:03-DPB1*46:01', 'HLA-DPA1*01:03-DPB1*47:01', 'HLA-DPA1*01:03-DPB1*48:01',
         'HLA-DPA1*01:03-DPB1*49:01', 'HLA-DPA1*01:03-DPB1*50:01',
         'HLA-DPA1*01:03-DPB1*51:01', 'HLA-DPA1*01:03-DPB1*52:01', 'HLA-DPA1*01:03-DPB1*53:01', 'HLA-DPA1*01:03-DPB1*54:01',
         'HLA-DPA1*01:03-DPB1*55:01', 'HLA-DPA1*01:03-DPB1*56:01',
         'HLA-DPA1*01:03-DPB1*58:01', 'HLA-DPA1*01:03-DPB1*59:01', 'HLA-DPA1*01:03-DPB1*60:01', 'HLA-DPA1*01:03-DPB1*62:01',
         'HLA-DPA1*01:03-DPB1*63:01', 'HLA-DPA1*01:03-DPB1*65:01',
         'HLA-DPA1*01:03-DPB1*66:01', 'HLA-DPA1*01:03-DPB1*67:01', 'HLA-DPA1*01:03-DPB1*68:01', 'HLA-DPA1*01:03-DPB1*69:01',
         'HLA-DPA1*01:03-DPB1*70:01', 'HLA-DPA1*01:03-DPB1*71:01',
         'HLA-DPA1*01:03-DPB1*72:01', 'HLA-DPA1*01:03-DPB1*73:01', 'HLA-DPA1*01:03-DPB1*74:01', 'HLA-DPA1*01:03-DPB1*75:01',
         'HLA-DPA1*01:03-DPB1*76:01', 'HLA-DPA1*01:03-DPB1*77:01',
         'HLA-DPA1*01:03-DPB1*78:01', 'HLA-DPA1*01:03-DPB1*79:01', 'HLA-DPA1*01:03-DPB1*80:01', 'HLA-DPA1*01:03-DPB1*81:01',
         'HLA-DPA1*01:03-DPB1*82:01', 'HLA-DPA1*01:03-DPB1*83:01',
         'HLA-DPA1*01:03-DPB1*84:01', 'HLA-DPA1*01:03-DPB1*85:01', 'HLA-DPA1*01:03-DPB1*86:01', 'HLA-DPA1*01:03-DPB1*87:01',
         'HLA-DPA1*01:03-DPB1*88:01', 'HLA-DPA1*01:03-DPB1*89:01',
         'HLA-DPA1*01:03-DPB1*90:01', 'HLA-DPA1*01:03-DPB1*91:01', 'HLA-DPA1*01:03-DPB1*92:01', 'HLA-DPA1*01:03-DPB1*93:01',
         'HLA-DPA1*01:03-DPB1*94:01', 'HLA-DPA1*01:03-DPB1*95:01',
         'HLA-DPA1*01:03-DPB1*96:01', 'HLA-DPA1*01:03-DPB1*97:01', 'HLA-DPA1*01:03-DPB1*98:01', 'HLA-DPA1*01:03-DPB1*99:01',
         'HLA-DPA1*01:04-DPB1*01:01', 'HLA-DPA1*01:04-DPB1*02:01',
         'HLA-DPA1*01:04-DPB1*02:02', 'HLA-DPA1*01:04-DPB1*03:01', 'HLA-DPA1*01:04-DPB1*04:01', 'HLA-DPA1*01:04-DPB1*04:02',
         'HLA-DPA1*01:04-DPB1*05:01', 'HLA-DPA1*01:04-DPB1*06:01',
         'HLA-DPA1*01:04-DPB1*08:01', 'HLA-DPA1*01:04-DPB1*09:01', 'HLA-DPA1*01:04-DPB1*10:001', 'HLA-DPA1*01:04-DPB1*10:01',
         'HLA-DPA1*01:04-DPB1*10:101', 'HLA-DPA1*01:04-DPB1*10:201',
         'HLA-DPA1*01:04-DPB1*10:301', 'HLA-DPA1*01:04-DPB1*10:401', 'HLA-DPA1*01:04-DPB1*10:501', 'HLA-DPA1*01:04-DPB1*10:601',
         'HLA-DPA1*01:04-DPB1*10:701', 'HLA-DPA1*01:04-DPB1*10:801',
         'HLA-DPA1*01:04-DPB1*10:901', 'HLA-DPA1*01:04-DPB1*11:001', 'HLA-DPA1*01:04-DPB1*11:01', 'HLA-DPA1*01:04-DPB1*11:101',
         'HLA-DPA1*01:04-DPB1*11:201', 'HLA-DPA1*01:04-DPB1*11:301',
         'HLA-DPA1*01:04-DPB1*11:401', 'HLA-DPA1*01:04-DPB1*11:501', 'HLA-DPA1*01:04-DPB1*11:601', 'HLA-DPA1*01:04-DPB1*11:701',
         'HLA-DPA1*01:04-DPB1*11:801', 'HLA-DPA1*01:04-DPB1*11:901',
         'HLA-DPA1*01:04-DPB1*12:101', 'HLA-DPA1*01:04-DPB1*12:201', 'HLA-DPA1*01:04-DPB1*12:301', 'HLA-DPA1*01:04-DPB1*12:401',
         'HLA-DPA1*01:04-DPB1*12:501', 'HLA-DPA1*01:04-DPB1*12:601',
         'HLA-DPA1*01:04-DPB1*12:701', 'HLA-DPA1*01:04-DPB1*12:801', 'HLA-DPA1*01:04-DPB1*12:901', 'HLA-DPA1*01:04-DPB1*13:001',
         'HLA-DPA1*01:04-DPB1*13:01', 'HLA-DPA1*01:04-DPB1*13:101',
         'HLA-DPA1*01:04-DPB1*13:201', 'HLA-DPA1*01:04-DPB1*13:301', 'HLA-DPA1*01:04-DPB1*13:401', 'HLA-DPA1*01:04-DPB1*14:01',
         'HLA-DPA1*01:04-DPB1*15:01', 'HLA-DPA1*01:04-DPB1*16:01',
         'HLA-DPA1*01:04-DPB1*17:01', 'HLA-DPA1*01:04-DPB1*18:01', 'HLA-DPA1*01:04-DPB1*19:01', 'HLA-DPA1*01:04-DPB1*20:01',
         'HLA-DPA1*01:04-DPB1*21:01', 'HLA-DPA1*01:04-DPB1*22:01',
         'HLA-DPA1*01:04-DPB1*23:01', 'HLA-DPA1*01:04-DPB1*24:01', 'HLA-DPA1*01:04-DPB1*25:01', 'HLA-DPA1*01:04-DPB1*26:01',
         'HLA-DPA1*01:04-DPB1*27:01', 'HLA-DPA1*01:04-DPB1*28:01',
         'HLA-DPA1*01:04-DPB1*29:01', 'HLA-DPA1*01:04-DPB1*30:01', 'HLA-DPA1*01:04-DPB1*31:01', 'HLA-DPA1*01:04-DPB1*32:01',
         'HLA-DPA1*01:04-DPB1*33:01', 'HLA-DPA1*01:04-DPB1*34:01',
         'HLA-DPA1*01:04-DPB1*35:01', 'HLA-DPA1*01:04-DPB1*36:01', 'HLA-DPA1*01:04-DPB1*37:01', 'HLA-DPA1*01:04-DPB1*38:01',
         'HLA-DPA1*01:04-DPB1*39:01', 'HLA-DPA1*01:04-DPB1*40:01',
         'HLA-DPA1*01:04-DPB1*41:01', 'HLA-DPA1*01:04-DPB1*44:01', 'HLA-DPA1*01:04-DPB1*45:01', 'HLA-DPA1*01:04-DPB1*46:01',
         'HLA-DPA1*01:04-DPB1*47:01', 'HLA-DPA1*01:04-DPB1*48:01',
         'HLA-DPA1*01:04-DPB1*49:01', 'HLA-DPA1*01:04-DPB1*50:01', 'HLA-DPA1*01:04-DPB1*51:01', 'HLA-DPA1*01:04-DPB1*52:01',
         'HLA-DPA1*01:04-DPB1*53:01', 'HLA-DPA1*01:04-DPB1*54:01',
         'HLA-DPA1*01:04-DPB1*55:01', 'HLA-DPA1*01:04-DPB1*56:01', 'HLA-DPA1*01:04-DPB1*58:01', 'HLA-DPA1*01:04-DPB1*59:01',
         'HLA-DPA1*01:04-DPB1*60:01', 'HLA-DPA1*01:04-DPB1*62:01',
         'HLA-DPA1*01:04-DPB1*63:01', 'HLA-DPA1*01:04-DPB1*65:01', 'HLA-DPA1*01:04-DPB1*66:01', 'HLA-DPA1*01:04-DPB1*67:01',
         'HLA-DPA1*01:04-DPB1*68:01', 'HLA-DPA1*01:04-DPB1*69:01',
         'HLA-DPA1*01:04-DPB1*70:01', 'HLA-DPA1*01:04-DPB1*71:01', 'HLA-DPA1*01:04-DPB1*72:01', 'HLA-DPA1*01:04-DPB1*73:01',
         'HLA-DPA1*01:04-DPB1*74:01', 'HLA-DPA1*01:04-DPB1*75:01',
         'HLA-DPA1*01:04-DPB1*76:01', 'HLA-DPA1*01:04-DPB1*77:01', 'HLA-DPA1*01:04-DPB1*78:01', 'HLA-DPA1*01:04-DPB1*79:01',
         'HLA-DPA1*01:04-DPB1*80:01', 'HLA-DPA1*01:04-DPB1*81:01',
         'HLA-DPA1*01:04-DPB1*82:01', 'HLA-DPA1*01:04-DPB1*83:01', 'HLA-DPA1*01:04-DPB1*84:01', 'HLA-DPA1*01:04-DPB1*85:01',
         'HLA-DPA1*01:04-DPB1*86:01', 'HLA-DPA1*01:04-DPB1*87:01',
         'HLA-DPA1*01:04-DPB1*88:01', 'HLA-DPA1*01:04-DPB1*89:01', 'HLA-DPA1*01:04-DPB1*90:01', 'HLA-DPA1*01:04-DPB1*91:01',
         'HLA-DPA1*01:04-DPB1*92:01', 'HLA-DPA1*01:04-DPB1*93:01',
         'HLA-DPA1*01:04-DPB1*94:01', 'HLA-DPA1*01:04-DPB1*95:01', 'HLA-DPA1*01:04-DPB1*96:01', 'HLA-DPA1*01:04-DPB1*97:01',
         'HLA-DPA1*01:04-DPB1*98:01', 'HLA-DPA1*01:04-DPB1*99:01',
         'HLA-DPA1*01:05-DPB1*01:01', 'HLA-DPA1*01:05-DPB1*02:01', 'HLA-DPA1*01:05-DPB1*02:02', 'HLA-DPA1*01:05-DPB1*03:01',
         'HLA-DPA1*01:05-DPB1*04:01', 'HLA-DPA1*01:05-DPB1*04:02',
         'HLA-DPA1*01:05-DPB1*05:01', 'HLA-DPA1*01:05-DPB1*06:01', 'HLA-DPA1*01:05-DPB1*08:01', 'HLA-DPA1*01:05-DPB1*09:01',
         'HLA-DPA1*01:05-DPB1*10:001', 'HLA-DPA1*01:05-DPB1*10:01',
         'HLA-DPA1*01:05-DPB1*10:101', 'HLA-DPA1*01:05-DPB1*10:201', 'HLA-DPA1*01:05-DPB1*10:301', 'HLA-DPA1*01:05-DPB1*10:401',
         'HLA-DPA1*01:05-DPB1*10:501', 'HLA-DPA1*01:05-DPB1*10:601',
         'HLA-DPA1*01:05-DPB1*10:701', 'HLA-DPA1*01:05-DPB1*10:801', 'HLA-DPA1*01:05-DPB1*10:901', 'HLA-DPA1*01:05-DPB1*11:001',
         'HLA-DPA1*01:05-DPB1*11:01', 'HLA-DPA1*01:05-DPB1*11:101',
         'HLA-DPA1*01:05-DPB1*11:201', 'HLA-DPA1*01:05-DPB1*11:301', 'HLA-DPA1*01:05-DPB1*11:401', 'HLA-DPA1*01:05-DPB1*11:501',
         'HLA-DPA1*01:05-DPB1*11:601', 'HLA-DPA1*01:05-DPB1*11:701',
         'HLA-DPA1*01:05-DPB1*11:801', 'HLA-DPA1*01:05-DPB1*11:901', 'HLA-DPA1*01:05-DPB1*12:101', 'HLA-DPA1*01:05-DPB1*12:201',
         'HLA-DPA1*01:05-DPB1*12:301', 'HLA-DPA1*01:05-DPB1*12:401',
         'HLA-DPA1*01:05-DPB1*12:501', 'HLA-DPA1*01:05-DPB1*12:601', 'HLA-DPA1*01:05-DPB1*12:701', 'HLA-DPA1*01:05-DPB1*12:801',
         'HLA-DPA1*01:05-DPB1*12:901', 'HLA-DPA1*01:05-DPB1*13:001',
         'HLA-DPA1*01:05-DPB1*13:01', 'HLA-DPA1*01:05-DPB1*13:101', 'HLA-DPA1*01:05-DPB1*13:201', 'HLA-DPA1*01:05-DPB1*13:301',
         'HLA-DPA1*01:05-DPB1*13:401', 'HLA-DPA1*01:05-DPB1*14:01',
         'HLA-DPA1*01:05-DPB1*15:01', 'HLA-DPA1*01:05-DPB1*16:01', 'HLA-DPA1*01:05-DPB1*17:01', 'HLA-DPA1*01:05-DPB1*18:01',
         'HLA-DPA1*01:05-DPB1*19:01', 'HLA-DPA1*01:05-DPB1*20:01',
         'HLA-DPA1*01:05-DPB1*21:01', 'HLA-DPA1*01:05-DPB1*22:01', 'HLA-DPA1*01:05-DPB1*23:01', 'HLA-DPA1*01:05-DPB1*24:01',
         'HLA-DPA1*01:05-DPB1*25:01', 'HLA-DPA1*01:05-DPB1*26:01',
         'HLA-DPA1*01:05-DPB1*27:01', 'HLA-DPA1*01:05-DPB1*28:01', 'HLA-DPA1*01:05-DPB1*29:01', 'HLA-DPA1*01:05-DPB1*30:01',
         'HLA-DPA1*01:05-DPB1*31:01', 'HLA-DPA1*01:05-DPB1*32:01',
         'HLA-DPA1*01:05-DPB1*33:01', 'HLA-DPA1*01:05-DPB1*34:01', 'HLA-DPA1*01:05-DPB1*35:01', 'HLA-DPA1*01:05-DPB1*36:01',
         'HLA-DPA1*01:05-DPB1*37:01', 'HLA-DPA1*01:05-DPB1*38:01',
         'HLA-DPA1*01:05-DPB1*39:01', 'HLA-DPA1*01:05-DPB1*40:01', 'HLA-DPA1*01:05-DPB1*41:01', 'HLA-DPA1*01:05-DPB1*44:01',
         'HLA-DPA1*01:05-DPB1*45:01', 'HLA-DPA1*01:05-DPB1*46:01',
         'HLA-DPA1*01:05-DPB1*47:01', 'HLA-DPA1*01:05-DPB1*48:01', 'HLA-DPA1*01:05-DPB1*49:01', 'HLA-DPA1*01:05-DPB1*50:01',
         'HLA-DPA1*01:05-DPB1*51:01', 'HLA-DPA1*01:05-DPB1*52:01',
         'HLA-DPA1*01:05-DPB1*53:01', 'HLA-DPA1*01:05-DPB1*54:01', 'HLA-DPA1*01:05-DPB1*55:01', 'HLA-DPA1*01:05-DPB1*56:01',
         'HLA-DPA1*01:05-DPB1*58:01', 'HLA-DPA1*01:05-DPB1*59:01',
         'HLA-DPA1*01:05-DPB1*60:01', 'HLA-DPA1*01:05-DPB1*62:01', 'HLA-DPA1*01:05-DPB1*63:01', 'HLA-DPA1*01:05-DPB1*65:01',
         'HLA-DPA1*01:05-DPB1*66:01', 'HLA-DPA1*01:05-DPB1*67:01',
         'HLA-DPA1*01:05-DPB1*68:01', 'HLA-DPA1*01:05-DPB1*69:01', 'HLA-DPA1*01:05-DPB1*70:01', 'HLA-DPA1*01:05-DPB1*71:01',
         'HLA-DPA1*01:05-DPB1*72:01', 'HLA-DPA1*01:05-DPB1*73:01',
         'HLA-DPA1*01:05-DPB1*74:01', 'HLA-DPA1*01:05-DPB1*75:01', 'HLA-DPA1*01:05-DPB1*76:01', 'HLA-DPA1*01:05-DPB1*77:01',
         'HLA-DPA1*01:05-DPB1*78:01', 'HLA-DPA1*01:05-DPB1*79:01',
         'HLA-DPA1*01:05-DPB1*80:01', 'HLA-DPA1*01:05-DPB1*81:01', 'HLA-DPA1*01:05-DPB1*82:01', 'HLA-DPA1*01:05-DPB1*83:01',
         'HLA-DPA1*01:05-DPB1*84:01', 'HLA-DPA1*01:05-DPB1*85:01',
         'HLA-DPA1*01:05-DPB1*86:01', 'HLA-DPA1*01:05-DPB1*87:01', 'HLA-DPA1*01:05-DPB1*88:01', 'HLA-DPA1*01:05-DPB1*89:01',
         'HLA-DPA1*01:05-DPB1*90:01', 'HLA-DPA1*01:05-DPB1*91:01',
         'HLA-DPA1*01:05-DPB1*92:01', 'HLA-DPA1*01:05-DPB1*93:01', 'HLA-DPA1*01:05-DPB1*94:01', 'HLA-DPA1*01:05-DPB1*95:01',
         'HLA-DPA1*01:05-DPB1*96:01', 'HLA-DPA1*01:05-DPB1*97:01',
         'HLA-DPA1*01:05-DPB1*98:01', 'HLA-DPA1*01:05-DPB1*99:01', 'HLA-DPA1*01:06-DPB1*01:01', 'HLA-DPA1*01:06-DPB1*02:01',
         'HLA-DPA1*01:06-DPB1*02:02', 'HLA-DPA1*01:06-DPB1*03:01',
         'HLA-DPA1*01:06-DPB1*04:01', 'HLA-DPA1*01:06-DPB1*04:02', 'HLA-DPA1*01:06-DPB1*05:01', 'HLA-DPA1*01:06-DPB1*06:01',
         'HLA-DPA1*01:06-DPB1*08:01', 'HLA-DPA1*01:06-DPB1*09:01',
         'HLA-DPA1*01:06-DPB1*10:001', 'HLA-DPA1*01:06-DPB1*10:01', 'HLA-DPA1*01:06-DPB1*10:101', 'HLA-DPA1*01:06-DPB1*10:201',
         'HLA-DPA1*01:06-DPB1*10:301', 'HLA-DPA1*01:06-DPB1*10:401',
         'HLA-DPA1*01:06-DPB1*10:501', 'HLA-DPA1*01:06-DPB1*10:601', 'HLA-DPA1*01:06-DPB1*10:701', 'HLA-DPA1*01:06-DPB1*10:801',
         'HLA-DPA1*01:06-DPB1*10:901', 'HLA-DPA1*01:06-DPB1*11:001',
         'HLA-DPA1*01:06-DPB1*11:01', 'HLA-DPA1*01:06-DPB1*11:101', 'HLA-DPA1*01:06-DPB1*11:201', 'HLA-DPA1*01:06-DPB1*11:301',
         'HLA-DPA1*01:06-DPB1*11:401', 'HLA-DPA1*01:06-DPB1*11:501',
         'HLA-DPA1*01:06-DPB1*11:601', 'HLA-DPA1*01:06-DPB1*11:701', 'HLA-DPA1*01:06-DPB1*11:801', 'HLA-DPA1*01:06-DPB1*11:901',
         'HLA-DPA1*01:06-DPB1*12:101', 'HLA-DPA1*01:06-DPB1*12:201',
         'HLA-DPA1*01:06-DPB1*12:301', 'HLA-DPA1*01:06-DPB1*12:401', 'HLA-DPA1*01:06-DPB1*12:501', 'HLA-DPA1*01:06-DPB1*12:601',
         'HLA-DPA1*01:06-DPB1*12:701', 'HLA-DPA1*01:06-DPB1*12:801',
         'HLA-DPA1*01:06-DPB1*12:901', 'HLA-DPA1*01:06-DPB1*13:001', 'HLA-DPA1*01:06-DPB1*13:01', 'HLA-DPA1*01:06-DPB1*13:101',
         'HLA-DPA1*01:06-DPB1*13:201', 'HLA-DPA1*01:06-DPB1*13:301',
         'HLA-DPA1*01:06-DPB1*13:401', 'HLA-DPA1*01:06-DPB1*14:01', 'HLA-DPA1*01:06-DPB1*15:01', 'HLA-DPA1*01:06-DPB1*16:01',
         'HLA-DPA1*01:06-DPB1*17:01', 'HLA-DPA1*01:06-DPB1*18:01',
         'HLA-DPA1*01:06-DPB1*19:01', 'HLA-DPA1*01:06-DPB1*20:01', 'HLA-DPA1*01:06-DPB1*21:01', 'HLA-DPA1*01:06-DPB1*22:01',
         'HLA-DPA1*01:06-DPB1*23:01', 'HLA-DPA1*01:06-DPB1*24:01',
         'HLA-DPA1*01:06-DPB1*25:01', 'HLA-DPA1*01:06-DPB1*26:01', 'HLA-DPA1*01:06-DPB1*27:01', 'HLA-DPA1*01:06-DPB1*28:01',
         'HLA-DPA1*01:06-DPB1*29:01', 'HLA-DPA1*01:06-DPB1*30:01',
         'HLA-DPA1*01:06-DPB1*31:01', 'HLA-DPA1*01:06-DPB1*32:01', 'HLA-DPA1*01:06-DPB1*33:01', 'HLA-DPA1*01:06-DPB1*34:01',
         'HLA-DPA1*01:06-DPB1*35:01', 'HLA-DPA1*01:06-DPB1*36:01',
         'HLA-DPA1*01:06-DPB1*37:01', 'HLA-DPA1*01:06-DPB1*38:01', 'HLA-DPA1*01:06-DPB1*39:01', 'HLA-DPA1*01:06-DPB1*40:01',
         'HLA-DPA1*01:06-DPB1*41:01', 'HLA-DPA1*01:06-DPB1*44:01',
         'HLA-DPA1*01:06-DPB1*45:01', 'HLA-DPA1*01:06-DPB1*46:01', 'HLA-DPA1*01:06-DPB1*47:01', 'HLA-DPA1*01:06-DPB1*48:01',
         'HLA-DPA1*01:06-DPB1*49:01', 'HLA-DPA1*01:06-DPB1*50:01',
         'HLA-DPA1*01:06-DPB1*51:01', 'HLA-DPA1*01:06-DPB1*52:01', 'HLA-DPA1*01:06-DPB1*53:01', 'HLA-DPA1*01:06-DPB1*54:01',
         'HLA-DPA1*01:06-DPB1*55:01', 'HLA-DPA1*01:06-DPB1*56:01',
         'HLA-DPA1*01:06-DPB1*58:01', 'HLA-DPA1*01:06-DPB1*59:01', 'HLA-DPA1*01:06-DPB1*60:01', 'HLA-DPA1*01:06-DPB1*62:01',
         'HLA-DPA1*01:06-DPB1*63:01', 'HLA-DPA1*01:06-DPB1*65:01',
         'HLA-DPA1*01:06-DPB1*66:01', 'HLA-DPA1*01:06-DPB1*67:01', 'HLA-DPA1*01:06-DPB1*68:01', 'HLA-DPA1*01:06-DPB1*69:01',
         'HLA-DPA1*01:06-DPB1*70:01', 'HLA-DPA1*01:06-DPB1*71:01',
         'HLA-DPA1*01:06-DPB1*72:01', 'HLA-DPA1*01:06-DPB1*73:01', 'HLA-DPA1*01:06-DPB1*74:01', 'HLA-DPA1*01:06-DPB1*75:01',
         'HLA-DPA1*01:06-DPB1*76:01', 'HLA-DPA1*01:06-DPB1*77:01',
         'HLA-DPA1*01:06-DPB1*78:01', 'HLA-DPA1*01:06-DPB1*79:01', 'HLA-DPA1*01:06-DPB1*80:01', 'HLA-DPA1*01:06-DPB1*81:01',
         'HLA-DPA1*01:06-DPB1*82:01', 'HLA-DPA1*01:06-DPB1*83:01',
         'HLA-DPA1*01:06-DPB1*84:01', 'HLA-DPA1*01:06-DPB1*85:01', 'HLA-DPA1*01:06-DPB1*86:01', 'HLA-DPA1*01:06-DPB1*87:01',
         'HLA-DPA1*01:06-DPB1*88:01', 'HLA-DPA1*01:06-DPB1*89:01',
         'HLA-DPA1*01:06-DPB1*90:01', 'HLA-DPA1*01:06-DPB1*91:01', 'HLA-DPA1*01:06-DPB1*92:01', 'HLA-DPA1*01:06-DPB1*93:01',
         'HLA-DPA1*01:06-DPB1*94:01', 'HLA-DPA1*01:06-DPB1*95:01',
         'HLA-DPA1*01:06-DPB1*96:01', 'HLA-DPA1*01:06-DPB1*97:01', 'HLA-DPA1*01:06-DPB1*98:01', 'HLA-DPA1*01:06-DPB1*99:01',
         'HLA-DPA1*01:07-DPB1*01:01', 'HLA-DPA1*01:07-DPB1*02:01',
         'HLA-DPA1*01:07-DPB1*02:02', 'HLA-DPA1*01:07-DPB1*03:01', 'HLA-DPA1*01:07-DPB1*04:01', 'HLA-DPA1*01:07-DPB1*04:02',
         'HLA-DPA1*01:07-DPB1*05:01', 'HLA-DPA1*01:07-DPB1*06:01',
         'HLA-DPA1*01:07-DPB1*08:01', 'HLA-DPA1*01:07-DPB1*09:01', 'HLA-DPA1*01:07-DPB1*10:001', 'HLA-DPA1*01:07-DPB1*10:01',
         'HLA-DPA1*01:07-DPB1*10:101', 'HLA-DPA1*01:07-DPB1*10:201',
         'HLA-DPA1*01:07-DPB1*10:301', 'HLA-DPA1*01:07-DPB1*10:401', 'HLA-DPA1*01:07-DPB1*10:501', 'HLA-DPA1*01:07-DPB1*10:601',
         'HLA-DPA1*01:07-DPB1*10:701', 'HLA-DPA1*01:07-DPB1*10:801',
         'HLA-DPA1*01:07-DPB1*10:901', 'HLA-DPA1*01:07-DPB1*11:001', 'HLA-DPA1*01:07-DPB1*11:01', 'HLA-DPA1*01:07-DPB1*11:101',
         'HLA-DPA1*01:07-DPB1*11:201', 'HLA-DPA1*01:07-DPB1*11:301',
         'HLA-DPA1*01:07-DPB1*11:401', 'HLA-DPA1*01:07-DPB1*11:501', 'HLA-DPA1*01:07-DPB1*11:601', 'HLA-DPA1*01:07-DPB1*11:701',
         'HLA-DPA1*01:07-DPB1*11:801', 'HLA-DPA1*01:07-DPB1*11:901',
         'HLA-DPA1*01:07-DPB1*12:101', 'HLA-DPA1*01:07-DPB1*12:201', 'HLA-DPA1*01:07-DPB1*12:301', 'HLA-DPA1*01:07-DPB1*12:401',
         'HLA-DPA1*01:07-DPB1*12:501', 'HLA-DPA1*01:07-DPB1*12:601',
         'HLA-DPA1*01:07-DPB1*12:701', 'HLA-DPA1*01:07-DPB1*12:801', 'HLA-DPA1*01:07-DPB1*12:901', 'HLA-DPA1*01:07-DPB1*13:001',
         'HLA-DPA1*01:07-DPB1*13:01', 'HLA-DPA1*01:07-DPB1*13:101',
         'HLA-DPA1*01:07-DPB1*13:201', 'HLA-DPA1*01:07-DPB1*13:301', 'HLA-DPA1*01:07-DPB1*13:401', 'HLA-DPA1*01:07-DPB1*14:01',
         'HLA-DPA1*01:07-DPB1*15:01', 'HLA-DPA1*01:07-DPB1*16:01',
         'HLA-DPA1*01:07-DPB1*17:01', 'HLA-DPA1*01:07-DPB1*18:01', 'HLA-DPA1*01:07-DPB1*19:01', 'HLA-DPA1*01:07-DPB1*20:01',
         'HLA-DPA1*01:07-DPB1*21:01', 'HLA-DPA1*01:07-DPB1*22:01',
         'HLA-DPA1*01:07-DPB1*23:01', 'HLA-DPA1*01:07-DPB1*24:01', 'HLA-DPA1*01:07-DPB1*25:01', 'HLA-DPA1*01:07-DPB1*26:01',
         'HLA-DPA1*01:07-DPB1*27:01', 'HLA-DPA1*01:07-DPB1*28:01',
         'HLA-DPA1*01:07-DPB1*29:01', 'HLA-DPA1*01:07-DPB1*30:01', 'HLA-DPA1*01:07-DPB1*31:01', 'HLA-DPA1*01:07-DPB1*32:01',
         'HLA-DPA1*01:07-DPB1*33:01', 'HLA-DPA1*01:07-DPB1*34:01',
         'HLA-DPA1*01:07-DPB1*35:01', 'HLA-DPA1*01:07-DPB1*36:01', 'HLA-DPA1*01:07-DPB1*37:01', 'HLA-DPA1*01:07-DPB1*38:01',
         'HLA-DPA1*01:07-DPB1*39:01', 'HLA-DPA1*01:07-DPB1*40:01',
         'HLA-DPA1*01:07-DPB1*41:01', 'HLA-DPA1*01:07-DPB1*44:01', 'HLA-DPA1*01:07-DPB1*45:01', 'HLA-DPA1*01:07-DPB1*46:01',
         'HLA-DPA1*01:07-DPB1*47:01', 'HLA-DPA1*01:07-DPB1*48:01',
         'HLA-DPA1*01:07-DPB1*49:01', 'HLA-DPA1*01:07-DPB1*50:01', 'HLA-DPA1*01:07-DPB1*51:01', 'HLA-DPA1*01:07-DPB1*52:01',
         'HLA-DPA1*01:07-DPB1*53:01', 'HLA-DPA1*01:07-DPB1*54:01',
         'HLA-DPA1*01:07-DPB1*55:01', 'HLA-DPA1*01:07-DPB1*56:01', 'HLA-DPA1*01:07-DPB1*58:01', 'HLA-DPA1*01:07-DPB1*59:01',
         'HLA-DPA1*01:07-DPB1*60:01', 'HLA-DPA1*01:07-DPB1*62:01',
         'HLA-DPA1*01:07-DPB1*63:01', 'HLA-DPA1*01:07-DPB1*65:01', 'HLA-DPA1*01:07-DPB1*66:01', 'HLA-DPA1*01:07-DPB1*67:01',
         'HLA-DPA1*01:07-DPB1*68:01', 'HLA-DPA1*01:07-DPB1*69:01',
         'HLA-DPA1*01:07-DPB1*70:01', 'HLA-DPA1*01:07-DPB1*71:01', 'HLA-DPA1*01:07-DPB1*72:01', 'HLA-DPA1*01:07-DPB1*73:01',
         'HLA-DPA1*01:07-DPB1*74:01', 'HLA-DPA1*01:07-DPB1*75:01',
         'HLA-DPA1*01:07-DPB1*76:01', 'HLA-DPA1*01:07-DPB1*77:01', 'HLA-DPA1*01:07-DPB1*78:01', 'HLA-DPA1*01:07-DPB1*79:01',
         'HLA-DPA1*01:07-DPB1*80:01', 'HLA-DPA1*01:07-DPB1*81:01',
         'HLA-DPA1*01:07-DPB1*82:01', 'HLA-DPA1*01:07-DPB1*83:01', 'HLA-DPA1*01:07-DPB1*84:01', 'HLA-DPA1*01:07-DPB1*85:01',
         'HLA-DPA1*01:07-DPB1*86:01', 'HLA-DPA1*01:07-DPB1*87:01',
         'HLA-DPA1*01:07-DPB1*88:01', 'HLA-DPA1*01:07-DPB1*89:01', 'HLA-DPA1*01:07-DPB1*90:01', 'HLA-DPA1*01:07-DPB1*91:01',
         'HLA-DPA1*01:07-DPB1*92:01', 'HLA-DPA1*01:07-DPB1*93:01',
         'HLA-DPA1*01:07-DPB1*94:01', 'HLA-DPA1*01:07-DPB1*95:01', 'HLA-DPA1*01:07-DPB1*96:01', 'HLA-DPA1*01:07-DPB1*97:01',
         'HLA-DPA1*01:07-DPB1*98:01', 'HLA-DPA1*01:07-DPB1*99:01',
         'HLA-DPA1*01:08-DPB1*01:01', 'HLA-DPA1*01:08-DPB1*02:01', 'HLA-DPA1*01:08-DPB1*02:02', 'HLA-DPA1*01:08-DPB1*03:01',
         'HLA-DPA1*01:08-DPB1*04:01', 'HLA-DPA1*01:08-DPB1*04:02',
         'HLA-DPA1*01:08-DPB1*05:01', 'HLA-DPA1*01:08-DPB1*06:01', 'HLA-DPA1*01:08-DPB1*08:01', 'HLA-DPA1*01:08-DPB1*09:01',
         'HLA-DPA1*01:08-DPB1*10:001', 'HLA-DPA1*01:08-DPB1*10:01',
         'HLA-DPA1*01:08-DPB1*10:101', 'HLA-DPA1*01:08-DPB1*10:201', 'HLA-DPA1*01:08-DPB1*10:301', 'HLA-DPA1*01:08-DPB1*10:401',
         'HLA-DPA1*01:08-DPB1*10:501', 'HLA-DPA1*01:08-DPB1*10:601',
         'HLA-DPA1*01:08-DPB1*10:701', 'HLA-DPA1*01:08-DPB1*10:801', 'HLA-DPA1*01:08-DPB1*10:901', 'HLA-DPA1*01:08-DPB1*11:001',
         'HLA-DPA1*01:08-DPB1*11:01', 'HLA-DPA1*01:08-DPB1*11:101',
         'HLA-DPA1*01:08-DPB1*11:201', 'HLA-DPA1*01:08-DPB1*11:301', 'HLA-DPA1*01:08-DPB1*11:401', 'HLA-DPA1*01:08-DPB1*11:501',
         'HLA-DPA1*01:08-DPB1*11:601', 'HLA-DPA1*01:08-DPB1*11:701',
         'HLA-DPA1*01:08-DPB1*11:801', 'HLA-DPA1*01:08-DPB1*11:901', 'HLA-DPA1*01:08-DPB1*12:101', 'HLA-DPA1*01:08-DPB1*12:201',
         'HLA-DPA1*01:08-DPB1*12:301', 'HLA-DPA1*01:08-DPB1*12:401',
         'HLA-DPA1*01:08-DPB1*12:501', 'HLA-DPA1*01:08-DPB1*12:601', 'HLA-DPA1*01:08-DPB1*12:701', 'HLA-DPA1*01:08-DPB1*12:801',
         'HLA-DPA1*01:08-DPB1*12:901', 'HLA-DPA1*01:08-DPB1*13:001',
         'HLA-DPA1*01:08-DPB1*13:01', 'HLA-DPA1*01:08-DPB1*13:101', 'HLA-DPA1*01:08-DPB1*13:201', 'HLA-DPA1*01:08-DPB1*13:301',
         'HLA-DPA1*01:08-DPB1*13:401', 'HLA-DPA1*01:08-DPB1*14:01',
         'HLA-DPA1*01:08-DPB1*15:01', 'HLA-DPA1*01:08-DPB1*16:01', 'HLA-DPA1*01:08-DPB1*17:01', 'HLA-DPA1*01:08-DPB1*18:01',
         'HLA-DPA1*01:08-DPB1*19:01', 'HLA-DPA1*01:08-DPB1*20:01',
         'HLA-DPA1*01:08-DPB1*21:01', 'HLA-DPA1*01:08-DPB1*22:01', 'HLA-DPA1*01:08-DPB1*23:01', 'HLA-DPA1*01:08-DPB1*24:01',
         'HLA-DPA1*01:08-DPB1*25:01', 'HLA-DPA1*01:08-DPB1*26:01',
         'HLA-DPA1*01:08-DPB1*27:01', 'HLA-DPA1*01:08-DPB1*28:01', 'HLA-DPA1*01:08-DPB1*29:01', 'HLA-DPA1*01:08-DPB1*30:01',
         'HLA-DPA1*01:08-DPB1*31:01', 'HLA-DPA1*01:08-DPB1*32:01',
         'HLA-DPA1*01:08-DPB1*33:01', 'HLA-DPA1*01:08-DPB1*34:01', 'HLA-DPA1*01:08-DPB1*35:01', 'HLA-DPA1*01:08-DPB1*36:01',
         'HLA-DPA1*01:08-DPB1*37:01', 'HLA-DPA1*01:08-DPB1*38:01',
         'HLA-DPA1*01:08-DPB1*39:01', 'HLA-DPA1*01:08-DPB1*40:01', 'HLA-DPA1*01:08-DPB1*41:01', 'HLA-DPA1*01:08-DPB1*44:01',
         'HLA-DPA1*01:08-DPB1*45:01', 'HLA-DPA1*01:08-DPB1*46:01',
         'HLA-DPA1*01:08-DPB1*47:01', 'HLA-DPA1*01:08-DPB1*48:01', 'HLA-DPA1*01:08-DPB1*49:01', 'HLA-DPA1*01:08-DPB1*50:01',
         'HLA-DPA1*01:08-DPB1*51:01', 'HLA-DPA1*01:08-DPB1*52:01',
         'HLA-DPA1*01:08-DPB1*53:01', 'HLA-DPA1*01:08-DPB1*54:01', 'HLA-DPA1*01:08-DPB1*55:01', 'HLA-DPA1*01:08-DPB1*56:01',
         'HLA-DPA1*01:08-DPB1*58:01', 'HLA-DPA1*01:08-DPB1*59:01',
         'HLA-DPA1*01:08-DPB1*60:01', 'HLA-DPA1*01:08-DPB1*62:01', 'HLA-DPA1*01:08-DPB1*63:01', 'HLA-DPA1*01:08-DPB1*65:01',
         'HLA-DPA1*01:08-DPB1*66:01', 'HLA-DPA1*01:08-DPB1*67:01',
         'HLA-DPA1*01:08-DPB1*68:01', 'HLA-DPA1*01:08-DPB1*69:01', 'HLA-DPA1*01:08-DPB1*70:01', 'HLA-DPA1*01:08-DPB1*71:01',
         'HLA-DPA1*01:08-DPB1*72:01', 'HLA-DPA1*01:08-DPB1*73:01',
         'HLA-DPA1*01:08-DPB1*74:01', 'HLA-DPA1*01:08-DPB1*75:01', 'HLA-DPA1*01:08-DPB1*76:01', 'HLA-DPA1*01:08-DPB1*77:01',
         'HLA-DPA1*01:08-DPB1*78:01', 'HLA-DPA1*01:08-DPB1*79:01',
         'HLA-DPA1*01:08-DPB1*80:01', 'HLA-DPA1*01:08-DPB1*81:01', 'HLA-DPA1*01:08-DPB1*82:01', 'HLA-DPA1*01:08-DPB1*83:01',
         'HLA-DPA1*01:08-DPB1*84:01', 'HLA-DPA1*01:08-DPB1*85:01',
         'HLA-DPA1*01:08-DPB1*86:01', 'HLA-DPA1*01:08-DPB1*87:01', 'HLA-DPA1*01:08-DPB1*88:01', 'HLA-DPA1*01:08-DPB1*89:01',
         'HLA-DPA1*01:08-DPB1*90:01', 'HLA-DPA1*01:08-DPB1*91:01',
         'HLA-DPA1*01:08-DPB1*92:01', 'HLA-DPA1*01:08-DPB1*93:01', 'HLA-DPA1*01:08-DPB1*94:01', 'HLA-DPA1*01:08-DPB1*95:01',
         'HLA-DPA1*01:08-DPB1*96:01', 'HLA-DPA1*01:08-DPB1*97:01',
         'HLA-DPA1*01:08-DPB1*98:01', 'HLA-DPA1*01:08-DPB1*99:01', 'HLA-DPA1*01:09-DPB1*01:01', 'HLA-DPA1*01:09-DPB1*02:01',
         'HLA-DPA1*01:09-DPB1*02:02', 'HLA-DPA1*01:09-DPB1*03:01',
         'HLA-DPA1*01:09-DPB1*04:01', 'HLA-DPA1*01:09-DPB1*04:02', 'HLA-DPA1*01:09-DPB1*05:01', 'HLA-DPA1*01:09-DPB1*06:01',
         'HLA-DPA1*01:09-DPB1*08:01', 'HLA-DPA1*01:09-DPB1*09:01',
         'HLA-DPA1*01:09-DPB1*10:001', 'HLA-DPA1*01:09-DPB1*10:01', 'HLA-DPA1*01:09-DPB1*10:101', 'HLA-DPA1*01:09-DPB1*10:201',
         'HLA-DPA1*01:09-DPB1*10:301', 'HLA-DPA1*01:09-DPB1*10:401',
         'HLA-DPA1*01:09-DPB1*10:501', 'HLA-DPA1*01:09-DPB1*10:601', 'HLA-DPA1*01:09-DPB1*10:701', 'HLA-DPA1*01:09-DPB1*10:801',
         'HLA-DPA1*01:09-DPB1*10:901', 'HLA-DPA1*01:09-DPB1*11:001',
         'HLA-DPA1*01:09-DPB1*11:01', 'HLA-DPA1*01:09-DPB1*11:101', 'HLA-DPA1*01:09-DPB1*11:201', 'HLA-DPA1*01:09-DPB1*11:301',
         'HLA-DPA1*01:09-DPB1*11:401', 'HLA-DPA1*01:09-DPB1*11:501',
         'HLA-DPA1*01:09-DPB1*11:601', 'HLA-DPA1*01:09-DPB1*11:701', 'HLA-DPA1*01:09-DPB1*11:801', 'HLA-DPA1*01:09-DPB1*11:901',
         'HLA-DPA1*01:09-DPB1*12:101', 'HLA-DPA1*01:09-DPB1*12:201',
         'HLA-DPA1*01:09-DPB1*12:301', 'HLA-DPA1*01:09-DPB1*12:401', 'HLA-DPA1*01:09-DPB1*12:501', 'HLA-DPA1*01:09-DPB1*12:601',
         'HLA-DPA1*01:09-DPB1*12:701', 'HLA-DPA1*01:09-DPB1*12:801',
         'HLA-DPA1*01:09-DPB1*12:901', 'HLA-DPA1*01:09-DPB1*13:001', 'HLA-DPA1*01:09-DPB1*13:01', 'HLA-DPA1*01:09-DPB1*13:101',
         'HLA-DPA1*01:09-DPB1*13:201', 'HLA-DPA1*01:09-DPB1*13:301',
         'HLA-DPA1*01:09-DPB1*13:401', 'HLA-DPA1*01:09-DPB1*14:01', 'HLA-DPA1*01:09-DPB1*15:01', 'HLA-DPA1*01:09-DPB1*16:01',
         'HLA-DPA1*01:09-DPB1*17:01', 'HLA-DPA1*01:09-DPB1*18:01',
         'HLA-DPA1*01:09-DPB1*19:01', 'HLA-DPA1*01:09-DPB1*20:01', 'HLA-DPA1*01:09-DPB1*21:01', 'HLA-DPA1*01:09-DPB1*22:01',
         'HLA-DPA1*01:09-DPB1*23:01', 'HLA-DPA1*01:09-DPB1*24:01',
         'HLA-DPA1*01:09-DPB1*25:01', 'HLA-DPA1*01:09-DPB1*26:01', 'HLA-DPA1*01:09-DPB1*27:01', 'HLA-DPA1*01:09-DPB1*28:01',
         'HLA-DPA1*01:09-DPB1*29:01', 'HLA-DPA1*01:09-DPB1*30:01',
         'HLA-DPA1*01:09-DPB1*31:01', 'HLA-DPA1*01:09-DPB1*32:01', 'HLA-DPA1*01:09-DPB1*33:01', 'HLA-DPA1*01:09-DPB1*34:01',
         'HLA-DPA1*01:09-DPB1*35:01', 'HLA-DPA1*01:09-DPB1*36:01',
         'HLA-DPA1*01:09-DPB1*37:01', 'HLA-DPA1*01:09-DPB1*38:01', 'HLA-DPA1*01:09-DPB1*39:01', 'HLA-DPA1*01:09-DPB1*40:01',
         'HLA-DPA1*01:09-DPB1*41:01', 'HLA-DPA1*01:09-DPB1*44:01',
         'HLA-DPA1*01:09-DPB1*45:01', 'HLA-DPA1*01:09-DPB1*46:01', 'HLA-DPA1*01:09-DPB1*47:01', 'HLA-DPA1*01:09-DPB1*48:01',
         'HLA-DPA1*01:09-DPB1*49:01', 'HLA-DPA1*01:09-DPB1*50:01',
         'HLA-DPA1*01:09-DPB1*51:01', 'HLA-DPA1*01:09-DPB1*52:01', 'HLA-DPA1*01:09-DPB1*53:01', 'HLA-DPA1*01:09-DPB1*54:01',
         'HLA-DPA1*01:09-DPB1*55:01', 'HLA-DPA1*01:09-DPB1*56:01',
         'HLA-DPA1*01:09-DPB1*58:01', 'HLA-DPA1*01:09-DPB1*59:01', 'HLA-DPA1*01:09-DPB1*60:01', 'HLA-DPA1*01:09-DPB1*62:01',
         'HLA-DPA1*01:09-DPB1*63:01', 'HLA-DPA1*01:09-DPB1*65:01',
         'HLA-DPA1*01:09-DPB1*66:01', 'HLA-DPA1*01:09-DPB1*67:01', 'HLA-DPA1*01:09-DPB1*68:01', 'HLA-DPA1*01:09-DPB1*69:01',
         'HLA-DPA1*01:09-DPB1*70:01', 'HLA-DPA1*01:09-DPB1*71:01',
         'HLA-DPA1*01:09-DPB1*72:01', 'HLA-DPA1*01:09-DPB1*73:01', 'HLA-DPA1*01:09-DPB1*74:01', 'HLA-DPA1*01:09-DPB1*75:01',
         'HLA-DPA1*01:09-DPB1*76:01', 'HLA-DPA1*01:09-DPB1*77:01',
         'HLA-DPA1*01:09-DPB1*78:01', 'HLA-DPA1*01:09-DPB1*79:01', 'HLA-DPA1*01:09-DPB1*80:01', 'HLA-DPA1*01:09-DPB1*81:01',
         'HLA-DPA1*01:09-DPB1*82:01', 'HLA-DPA1*01:09-DPB1*83:01',
         'HLA-DPA1*01:09-DPB1*84:01', 'HLA-DPA1*01:09-DPB1*85:01', 'HLA-DPA1*01:09-DPB1*86:01', 'HLA-DPA1*01:09-DPB1*87:01',
         'HLA-DPA1*01:09-DPB1*88:01', 'HLA-DPA1*01:09-DPB1*89:01',
         'HLA-DPA1*01:09-DPB1*90:01', 'HLA-DPA1*01:09-DPB1*91:01', 'HLA-DPA1*01:09-DPB1*92:01', 'HLA-DPA1*01:09-DPB1*93:01',
         'HLA-DPA1*01:09-DPB1*94:01', 'HLA-DPA1*01:09-DPB1*95:01',
         'HLA-DPA1*01:09-DPB1*96:01', 'HLA-DPA1*01:09-DPB1*97:01', 'HLA-DPA1*01:09-DPB1*98:01', 'HLA-DPA1*01:09-DPB1*99:01',
         'HLA-DPA1*01:10-DPB1*01:01', 'HLA-DPA1*01:10-DPB1*02:01',
         'HLA-DPA1*01:10-DPB1*02:02', 'HLA-DPA1*01:10-DPB1*03:01', 'HLA-DPA1*01:10-DPB1*04:01', 'HLA-DPA1*01:10-DPB1*04:02',
         'HLA-DPA1*01:10-DPB1*05:01', 'HLA-DPA1*01:10-DPB1*06:01',
         'HLA-DPA1*01:10-DPB1*08:01', 'HLA-DPA1*01:10-DPB1*09:01', 'HLA-DPA1*01:10-DPB1*10:001', 'HLA-DPA1*01:10-DPB1*10:01',
         'HLA-DPA1*01:10-DPB1*10:101', 'HLA-DPA1*01:10-DPB1*10:201',
         'HLA-DPA1*01:10-DPB1*10:301', 'HLA-DPA1*01:10-DPB1*10:401', 'HLA-DPA1*01:10-DPB1*10:501', 'HLA-DPA1*01:10-DPB1*10:601',
         'HLA-DPA1*01:10-DPB1*10:701', 'HLA-DPA1*01:10-DPB1*10:801',
         'HLA-DPA1*01:10-DPB1*10:901', 'HLA-DPA1*01:10-DPB1*11:001', 'HLA-DPA1*01:10-DPB1*11:01', 'HLA-DPA1*01:10-DPB1*11:101',
         'HLA-DPA1*01:10-DPB1*11:201', 'HLA-DPA1*01:10-DPB1*11:301',
         'HLA-DPA1*01:10-DPB1*11:401', 'HLA-DPA1*01:10-DPB1*11:501', 'HLA-DPA1*01:10-DPB1*11:601', 'HLA-DPA1*01:10-DPB1*11:701',
         'HLA-DPA1*01:10-DPB1*11:801', 'HLA-DPA1*01:10-DPB1*11:901',
         'HLA-DPA1*01:10-DPB1*12:101', 'HLA-DPA1*01:10-DPB1*12:201', 'HLA-DPA1*01:10-DPB1*12:301', 'HLA-DPA1*01:10-DPB1*12:401',
         'HLA-DPA1*01:10-DPB1*12:501', 'HLA-DPA1*01:10-DPB1*12:601',
         'HLA-DPA1*01:10-DPB1*12:701', 'HLA-DPA1*01:10-DPB1*12:801', 'HLA-DPA1*01:10-DPB1*12:901', 'HLA-DPA1*01:10-DPB1*13:001',
         'HLA-DPA1*01:10-DPB1*13:01', 'HLA-DPA1*01:10-DPB1*13:101',
         'HLA-DPA1*01:10-DPB1*13:201', 'HLA-DPA1*01:10-DPB1*13:301', 'HLA-DPA1*01:10-DPB1*13:401', 'HLA-DPA1*01:10-DPB1*14:01',
         'HLA-DPA1*01:10-DPB1*15:01', 'HLA-DPA1*01:10-DPB1*16:01',
         'HLA-DPA1*01:10-DPB1*17:01', 'HLA-DPA1*01:10-DPB1*18:01', 'HLA-DPA1*01:10-DPB1*19:01', 'HLA-DPA1*01:10-DPB1*20:01',
         'HLA-DPA1*01:10-DPB1*21:01', 'HLA-DPA1*01:10-DPB1*22:01',
         'HLA-DPA1*01:10-DPB1*23:01', 'HLA-DPA1*01:10-DPB1*24:01', 'HLA-DPA1*01:10-DPB1*25:01', 'HLA-DPA1*01:10-DPB1*26:01',
         'HLA-DPA1*01:10-DPB1*27:01', 'HLA-DPA1*01:10-DPB1*28:01',
         'HLA-DPA1*01:10-DPB1*29:01', 'HLA-DPA1*01:10-DPB1*30:01', 'HLA-DPA1*01:10-DPB1*31:01', 'HLA-DPA1*01:10-DPB1*32:01',
         'HLA-DPA1*01:10-DPB1*33:01', 'HLA-DPA1*01:10-DPB1*34:01',
         'HLA-DPA1*01:10-DPB1*35:01', 'HLA-DPA1*01:10-DPB1*36:01', 'HLA-DPA1*01:10-DPB1*37:01', 'HLA-DPA1*01:10-DPB1*38:01',
         'HLA-DPA1*01:10-DPB1*39:01', 'HLA-DPA1*01:10-DPB1*40:01',
         'HLA-DPA1*01:10-DPB1*41:01', 'HLA-DPA1*01:10-DPB1*44:01', 'HLA-DPA1*01:10-DPB1*45:01', 'HLA-DPA1*01:10-DPB1*46:01',
         'HLA-DPA1*01:10-DPB1*47:01', 'HLA-DPA1*01:10-DPB1*48:01',
         'HLA-DPA1*01:10-DPB1*49:01', 'HLA-DPA1*01:10-DPB1*50:01', 'HLA-DPA1*01:10-DPB1*51:01', 'HLA-DPA1*01:10-DPB1*52:01',
         'HLA-DPA1*01:10-DPB1*53:01', 'HLA-DPA1*01:10-DPB1*54:01',
         'HLA-DPA1*01:10-DPB1*55:01', 'HLA-DPA1*01:10-DPB1*56:01', 'HLA-DPA1*01:10-DPB1*58:01', 'HLA-DPA1*01:10-DPB1*59:01',
         'HLA-DPA1*01:10-DPB1*60:01', 'HLA-DPA1*01:10-DPB1*62:01',
         'HLA-DPA1*01:10-DPB1*63:01', 'HLA-DPA1*01:10-DPB1*65:01', 'HLA-DPA1*01:10-DPB1*66:01', 'HLA-DPA1*01:10-DPB1*67:01',
         'HLA-DPA1*01:10-DPB1*68:01', 'HLA-DPA1*01:10-DPB1*69:01',
         'HLA-DPA1*01:10-DPB1*70:01', 'HLA-DPA1*01:10-DPB1*71:01', 'HLA-DPA1*01:10-DPB1*72:01', 'HLA-DPA1*01:10-DPB1*73:01',
         'HLA-DPA1*01:10-DPB1*74:01', 'HLA-DPA1*01:10-DPB1*75:01',
         'HLA-DPA1*01:10-DPB1*76:01', 'HLA-DPA1*01:10-DPB1*77:01', 'HLA-DPA1*01:10-DPB1*78:01', 'HLA-DPA1*01:10-DPB1*79:01',
         'HLA-DPA1*01:10-DPB1*80:01', 'HLA-DPA1*01:10-DPB1*81:01',
         'HLA-DPA1*01:10-DPB1*82:01', 'HLA-DPA1*01:10-DPB1*83:01', 'HLA-DPA1*01:10-DPB1*84:01', 'HLA-DPA1*01:10-DPB1*85:01',
         'HLA-DPA1*01:10-DPB1*86:01', 'HLA-DPA1*01:10-DPB1*87:01',
         'HLA-DPA1*01:10-DPB1*88:01', 'HLA-DPA1*01:10-DPB1*89:01', 'HLA-DPA1*01:10-DPB1*90:01', 'HLA-DPA1*01:10-DPB1*91:01',
         'HLA-DPA1*01:10-DPB1*92:01', 'HLA-DPA1*01:10-DPB1*93:01',
         'HLA-DPA1*01:10-DPB1*94:01', 'HLA-DPA1*01:10-DPB1*95:01', 'HLA-DPA1*01:10-DPB1*96:01', 'HLA-DPA1*01:10-DPB1*97:01',
         'HLA-DPA1*01:10-DPB1*98:01', 'HLA-DPA1*01:10-DPB1*99:01',
         'HLA-DPA1*02:01-DPB1*01:01', 'HLA-DPA1*02:01-DPB1*02:01', 'HLA-DPA1*02:01-DPB1*02:02', 'HLA-DPA1*02:01-DPB1*03:01',
         'HLA-DPA1*02:01-DPB1*04:01', 'HLA-DPA1*02:01-DPB1*04:02',
         'HLA-DPA1*02:01-DPB1*05:01', 'HLA-DPA1*02:01-DPB1*06:01', 'HLA-DPA1*02:01-DPB1*08:01', 'HLA-DPA1*02:01-DPB1*09:01',
         'HLA-DPA1*02:01-DPB1*10:001', 'HLA-DPA1*02:01-DPB1*10:01',
         'HLA-DPA1*02:01-DPB1*10:101', 'HLA-DPA1*02:01-DPB1*10:201', 'HLA-DPA1*02:01-DPB1*10:301', 'HLA-DPA1*02:01-DPB1*10:401',
         'HLA-DPA1*02:01-DPB1*10:501', 'HLA-DPA1*02:01-DPB1*10:601',
         'HLA-DPA1*02:01-DPB1*10:701', 'HLA-DPA1*02:01-DPB1*10:801', 'HLA-DPA1*02:01-DPB1*10:901', 'HLA-DPA1*02:01-DPB1*11:001',
         'HLA-DPA1*02:01-DPB1*11:01', 'HLA-DPA1*02:01-DPB1*11:101',
         'HLA-DPA1*02:01-DPB1*11:201', 'HLA-DPA1*02:01-DPB1*11:301', 'HLA-DPA1*02:01-DPB1*11:401', 'HLA-DPA1*02:01-DPB1*11:501',
         'HLA-DPA1*02:01-DPB1*11:601', 'HLA-DPA1*02:01-DPB1*11:701',
         'HLA-DPA1*02:01-DPB1*11:801', 'HLA-DPA1*02:01-DPB1*11:901', 'HLA-DPA1*02:01-DPB1*12:101', 'HLA-DPA1*02:01-DPB1*12:201',
         'HLA-DPA1*02:01-DPB1*12:301', 'HLA-DPA1*02:01-DPB1*12:401',
         'HLA-DPA1*02:01-DPB1*12:501', 'HLA-DPA1*02:01-DPB1*12:601', 'HLA-DPA1*02:01-DPB1*12:701', 'HLA-DPA1*02:01-DPB1*12:801',
         'HLA-DPA1*02:01-DPB1*12:901', 'HLA-DPA1*02:01-DPB1*13:001',
         'HLA-DPA1*02:01-DPB1*13:01', 'HLA-DPA1*02:01-DPB1*13:101', 'HLA-DPA1*02:01-DPB1*13:201', 'HLA-DPA1*02:01-DPB1*13:301',
         'HLA-DPA1*02:01-DPB1*13:401', 'HLA-DPA1*02:01-DPB1*14:01',
         'HLA-DPA1*02:01-DPB1*15:01', 'HLA-DPA1*02:01-DPB1*16:01', 'HLA-DPA1*02:01-DPB1*17:01', 'HLA-DPA1*02:01-DPB1*18:01',
         'HLA-DPA1*02:01-DPB1*19:01', 'HLA-DPA1*02:01-DPB1*20:01',
         'HLA-DPA1*02:01-DPB1*21:01', 'HLA-DPA1*02:01-DPB1*22:01', 'HLA-DPA1*02:01-DPB1*23:01', 'HLA-DPA1*02:01-DPB1*24:01',
         'HLA-DPA1*02:01-DPB1*25:01', 'HLA-DPA1*02:01-DPB1*26:01',
         'HLA-DPA1*02:01-DPB1*27:01', 'HLA-DPA1*02:01-DPB1*28:01', 'HLA-DPA1*02:01-DPB1*29:01', 'HLA-DPA1*02:01-DPB1*30:01',
         'HLA-DPA1*02:01-DPB1*31:01', 'HLA-DPA1*02:01-DPB1*32:01',
         'HLA-DPA1*02:01-DPB1*33:01', 'HLA-DPA1*02:01-DPB1*34:01', 'HLA-DPA1*02:01-DPB1*35:01', 'HLA-DPA1*02:01-DPB1*36:01',
         'HLA-DPA1*02:01-DPB1*37:01', 'HLA-DPA1*02:01-DPB1*38:01',
         'HLA-DPA1*02:01-DPB1*39:01', 'HLA-DPA1*02:01-DPB1*40:01', 'HLA-DPA1*02:01-DPB1*41:01', 'HLA-DPA1*02:01-DPB1*44:01',
         'HLA-DPA1*02:01-DPB1*45:01', 'HLA-DPA1*02:01-DPB1*46:01',
         'HLA-DPA1*02:01-DPB1*47:01', 'HLA-DPA1*02:01-DPB1*48:01', 'HLA-DPA1*02:01-DPB1*49:01', 'HLA-DPA1*02:01-DPB1*50:01',
         'HLA-DPA1*02:01-DPB1*51:01', 'HLA-DPA1*02:01-DPB1*52:01',
         'HLA-DPA1*02:01-DPB1*53:01', 'HLA-DPA1*02:01-DPB1*54:01', 'HLA-DPA1*02:01-DPB1*55:01', 'HLA-DPA1*02:01-DPB1*56:01',
         'HLA-DPA1*02:01-DPB1*58:01', 'HLA-DPA1*02:01-DPB1*59:01',
         'HLA-DPA1*02:01-DPB1*60:01', 'HLA-DPA1*02:01-DPB1*62:01', 'HLA-DPA1*02:01-DPB1*63:01', 'HLA-DPA1*02:01-DPB1*65:01',
         'HLA-DPA1*02:01-DPB1*66:01', 'HLA-DPA1*02:01-DPB1*67:01',
         'HLA-DPA1*02:01-DPB1*68:01', 'HLA-DPA1*02:01-DPB1*69:01', 'HLA-DPA1*02:01-DPB1*70:01', 'HLA-DPA1*02:01-DPB1*71:01',
         'HLA-DPA1*02:01-DPB1*72:01', 'HLA-DPA1*02:01-DPB1*73:01',
         'HLA-DPA1*02:01-DPB1*74:01', 'HLA-DPA1*02:01-DPB1*75:01', 'HLA-DPA1*02:01-DPB1*76:01', 'HLA-DPA1*02:01-DPB1*77:01',
         'HLA-DPA1*02:01-DPB1*78:01', 'HLA-DPA1*02:01-DPB1*79:01',
         'HLA-DPA1*02:01-DPB1*80:01', 'HLA-DPA1*02:01-DPB1*81:01', 'HLA-DPA1*02:01-DPB1*82:01', 'HLA-DPA1*02:01-DPB1*83:01',
         'HLA-DPA1*02:01-DPB1*84:01', 'HLA-DPA1*02:01-DPB1*85:01',
         'HLA-DPA1*02:01-DPB1*86:01', 'HLA-DPA1*02:01-DPB1*87:01', 'HLA-DPA1*02:01-DPB1*88:01', 'HLA-DPA1*02:01-DPB1*89:01',
         'HLA-DPA1*02:01-DPB1*90:01', 'HLA-DPA1*02:01-DPB1*91:01',
         'HLA-DPA1*02:01-DPB1*92:01', 'HLA-DPA1*02:01-DPB1*93:01', 'HLA-DPA1*02:01-DPB1*94:01', 'HLA-DPA1*02:01-DPB1*95:01',
         'HLA-DPA1*02:01-DPB1*96:01', 'HLA-DPA1*02:01-DPB1*97:01',
         'HLA-DPA1*02:01-DPB1*98:01', 'HLA-DPA1*02:01-DPB1*99:01', 'HLA-DPA1*02:02-DPB1*01:01', 'HLA-DPA1*02:02-DPB1*02:01',
         'HLA-DPA1*02:02-DPB1*02:02', 'HLA-DPA1*02:02-DPB1*03:01',
         'HLA-DPA1*02:02-DPB1*04:01', 'HLA-DPA1*02:02-DPB1*04:02', 'HLA-DPA1*02:02-DPB1*05:01', 'HLA-DPA1*02:02-DPB1*06:01',
         'HLA-DPA1*02:02-DPB1*08:01', 'HLA-DPA1*02:02-DPB1*09:01',
         'HLA-DPA1*02:02-DPB1*10:001', 'HLA-DPA1*02:02-DPB1*10:01', 'HLA-DPA1*02:02-DPB1*10:101', 'HLA-DPA1*02:02-DPB1*10:201',
         'HLA-DPA1*02:02-DPB1*10:301', 'HLA-DPA1*02:02-DPB1*10:401',
         'HLA-DPA1*02:02-DPB1*10:501', 'HLA-DPA1*02:02-DPB1*10:601', 'HLA-DPA1*02:02-DPB1*10:701', 'HLA-DPA1*02:02-DPB1*10:801',
         'HLA-DPA1*02:02-DPB1*10:901', 'HLA-DPA1*02:02-DPB1*11:001',
         'HLA-DPA1*02:02-DPB1*11:01', 'HLA-DPA1*02:02-DPB1*11:101', 'HLA-DPA1*02:02-DPB1*11:201', 'HLA-DPA1*02:02-DPB1*11:301',
         'HLA-DPA1*02:02-DPB1*11:401', 'HLA-DPA1*02:02-DPB1*11:501',
         'HLA-DPA1*02:02-DPB1*11:601', 'HLA-DPA1*02:02-DPB1*11:701', 'HLA-DPA1*02:02-DPB1*11:801', 'HLA-DPA1*02:02-DPB1*11:901',
         'HLA-DPA1*02:02-DPB1*12:101', 'HLA-DPA1*02:02-DPB1*12:201',
         'HLA-DPA1*02:02-DPB1*12:301', 'HLA-DPA1*02:02-DPB1*12:401', 'HLA-DPA1*02:02-DPB1*12:501', 'HLA-DPA1*02:02-DPB1*12:601',
         'HLA-DPA1*02:02-DPB1*12:701', 'HLA-DPA1*02:02-DPB1*12:801',
         'HLA-DPA1*02:02-DPB1*12:901', 'HLA-DPA1*02:02-DPB1*13:001', 'HLA-DPA1*02:02-DPB1*13:01', 'HLA-DPA1*02:02-DPB1*13:101',
         'HLA-DPA1*02:02-DPB1*13:201', 'HLA-DPA1*02:02-DPB1*13:301',
         'HLA-DPA1*02:02-DPB1*13:401', 'HLA-DPA1*02:02-DPB1*14:01', 'HLA-DPA1*02:02-DPB1*15:01', 'HLA-DPA1*02:02-DPB1*16:01',
         'HLA-DPA1*02:02-DPB1*17:01', 'HLA-DPA1*02:02-DPB1*18:01',
         'HLA-DPA1*02:02-DPB1*19:01', 'HLA-DPA1*02:02-DPB1*20:01', 'HLA-DPA1*02:02-DPB1*21:01', 'HLA-DPA1*02:02-DPB1*22:01',
         'HLA-DPA1*02:02-DPB1*23:01', 'HLA-DPA1*02:02-DPB1*24:01',
         'HLA-DPA1*02:02-DPB1*25:01', 'HLA-DPA1*02:02-DPB1*26:01', 'HLA-DPA1*02:02-DPB1*27:01', 'HLA-DPA1*02:02-DPB1*28:01',
         'HLA-DPA1*02:02-DPB1*29:01', 'HLA-DPA1*02:02-DPB1*30:01',
         'HLA-DPA1*02:02-DPB1*31:01', 'HLA-DPA1*02:02-DPB1*32:01', 'HLA-DPA1*02:02-DPB1*33:01', 'HLA-DPA1*02:02-DPB1*34:01',
         'HLA-DPA1*02:02-DPB1*35:01', 'HLA-DPA1*02:02-DPB1*36:01',
         'HLA-DPA1*02:02-DPB1*37:01', 'HLA-DPA1*02:02-DPB1*38:01', 'HLA-DPA1*02:02-DPB1*39:01', 'HLA-DPA1*02:02-DPB1*40:01',
         'HLA-DPA1*02:02-DPB1*41:01', 'HLA-DPA1*02:02-DPB1*44:01',
         'HLA-DPA1*02:02-DPB1*45:01', 'HLA-DPA1*02:02-DPB1*46:01', 'HLA-DPA1*02:02-DPB1*47:01', 'HLA-DPA1*02:02-DPB1*48:01',
         'HLA-DPA1*02:02-DPB1*49:01', 'HLA-DPA1*02:02-DPB1*50:01',
         'HLA-DPA1*02:02-DPB1*51:01', 'HLA-DPA1*02:02-DPB1*52:01', 'HLA-DPA1*02:02-DPB1*53:01', 'HLA-DPA1*02:02-DPB1*54:01',
         'HLA-DPA1*02:02-DPB1*55:01', 'HLA-DPA1*02:02-DPB1*56:01',
         'HLA-DPA1*02:02-DPB1*58:01', 'HLA-DPA1*02:02-DPB1*59:01', 'HLA-DPA1*02:02-DPB1*60:01', 'HLA-DPA1*02:02-DPB1*62:01',
         'HLA-DPA1*02:02-DPB1*63:01', 'HLA-DPA1*02:02-DPB1*65:01',
         'HLA-DPA1*02:02-DPB1*66:01', 'HLA-DPA1*02:02-DPB1*67:01', 'HLA-DPA1*02:02-DPB1*68:01', 'HLA-DPA1*02:02-DPB1*69:01',
         'HLA-DPA1*02:02-DPB1*70:01', 'HLA-DPA1*02:02-DPB1*71:01',
         'HLA-DPA1*02:02-DPB1*72:01', 'HLA-DPA1*02:02-DPB1*73:01', 'HLA-DPA1*02:02-DPB1*74:01', 'HLA-DPA1*02:02-DPB1*75:01',
         'HLA-DPA1*02:02-DPB1*76:01', 'HLA-DPA1*02:02-DPB1*77:01',
         'HLA-DPA1*02:02-DPB1*78:01', 'HLA-DPA1*02:02-DPB1*79:01', 'HLA-DPA1*02:02-DPB1*80:01', 'HLA-DPA1*02:02-DPB1*81:01',
         'HLA-DPA1*02:02-DPB1*82:01', 'HLA-DPA1*02:02-DPB1*83:01',
         'HLA-DPA1*02:02-DPB1*84:01', 'HLA-DPA1*02:02-DPB1*85:01', 'HLA-DPA1*02:02-DPB1*86:01', 'HLA-DPA1*02:02-DPB1*87:01',
         'HLA-DPA1*02:02-DPB1*88:01', 'HLA-DPA1*02:02-DPB1*89:01',
         'HLA-DPA1*02:02-DPB1*90:01', 'HLA-DPA1*02:02-DPB1*91:01', 'HLA-DPA1*02:02-DPB1*92:01', 'HLA-DPA1*02:02-DPB1*93:01',
         'HLA-DPA1*02:02-DPB1*94:01', 'HLA-DPA1*02:02-DPB1*95:01',
         'HLA-DPA1*02:02-DPB1*96:01', 'HLA-DPA1*02:02-DPB1*97:01', 'HLA-DPA1*02:02-DPB1*98:01', 'HLA-DPA1*02:02-DPB1*99:01',
         'HLA-DPA1*02:03-DPB1*01:01', 'HLA-DPA1*02:03-DPB1*02:01',
         'HLA-DPA1*02:03-DPB1*02:02', 'HLA-DPA1*02:03-DPB1*03:01', 'HLA-DPA1*02:03-DPB1*04:01', 'HLA-DPA1*02:03-DPB1*04:02',
         'HLA-DPA1*02:03-DPB1*05:01', 'HLA-DPA1*02:03-DPB1*06:01',
         'HLA-DPA1*02:03-DPB1*08:01', 'HLA-DPA1*02:03-DPB1*09:01', 'HLA-DPA1*02:03-DPB1*10:001', 'HLA-DPA1*02:03-DPB1*10:01',
         'HLA-DPA1*02:03-DPB1*10:101', 'HLA-DPA1*02:03-DPB1*10:201',
         'HLA-DPA1*02:03-DPB1*10:301', 'HLA-DPA1*02:03-DPB1*10:401', 'HLA-DPA1*02:03-DPB1*10:501', 'HLA-DPA1*02:03-DPB1*10:601',
         'HLA-DPA1*02:03-DPB1*10:701', 'HLA-DPA1*02:03-DPB1*10:801',
         'HLA-DPA1*02:03-DPB1*10:901', 'HLA-DPA1*02:03-DPB1*11:001', 'HLA-DPA1*02:03-DPB1*11:01', 'HLA-DPA1*02:03-DPB1*11:101',
         'HLA-DPA1*02:03-DPB1*11:201', 'HLA-DPA1*02:03-DPB1*11:301',
         'HLA-DPA1*02:03-DPB1*11:401', 'HLA-DPA1*02:03-DPB1*11:501', 'HLA-DPA1*02:03-DPB1*11:601', 'HLA-DPA1*02:03-DPB1*11:701',
         'HLA-DPA1*02:03-DPB1*11:801', 'HLA-DPA1*02:03-DPB1*11:901',
         'HLA-DPA1*02:03-DPB1*12:101', 'HLA-DPA1*02:03-DPB1*12:201', 'HLA-DPA1*02:03-DPB1*12:301', 'HLA-DPA1*02:03-DPB1*12:401',
         'HLA-DPA1*02:03-DPB1*12:501', 'HLA-DPA1*02:03-DPB1*12:601',
         'HLA-DPA1*02:03-DPB1*12:701', 'HLA-DPA1*02:03-DPB1*12:801', 'HLA-DPA1*02:03-DPB1*12:901', 'HLA-DPA1*02:03-DPB1*13:001',
         'HLA-DPA1*02:03-DPB1*13:01', 'HLA-DPA1*02:03-DPB1*13:101',
         'HLA-DPA1*02:03-DPB1*13:201', 'HLA-DPA1*02:03-DPB1*13:301', 'HLA-DPA1*02:03-DPB1*13:401', 'HLA-DPA1*02:03-DPB1*14:01',
         'HLA-DPA1*02:03-DPB1*15:01', 'HLA-DPA1*02:03-DPB1*16:01',
         'HLA-DPA1*02:03-DPB1*17:01', 'HLA-DPA1*02:03-DPB1*18:01', 'HLA-DPA1*02:03-DPB1*19:01', 'HLA-DPA1*02:03-DPB1*20:01',
         'HLA-DPA1*02:03-DPB1*21:01', 'HLA-DPA1*02:03-DPB1*22:01',
         'HLA-DPA1*02:03-DPB1*23:01', 'HLA-DPA1*02:03-DPB1*24:01', 'HLA-DPA1*02:03-DPB1*25:01', 'HLA-DPA1*02:03-DPB1*26:01',
         'HLA-DPA1*02:03-DPB1*27:01', 'HLA-DPA1*02:03-DPB1*28:01',
         'HLA-DPA1*02:03-DPB1*29:01', 'HLA-DPA1*02:03-DPB1*30:01', 'HLA-DPA1*02:03-DPB1*31:01', 'HLA-DPA1*02:03-DPB1*32:01',
         'HLA-DPA1*02:03-DPB1*33:01', 'HLA-DPA1*02:03-DPB1*34:01',
         'HLA-DPA1*02:03-DPB1*35:01', 'HLA-DPA1*02:03-DPB1*36:01', 'HLA-DPA1*02:03-DPB1*37:01', 'HLA-DPA1*02:03-DPB1*38:01',
         'HLA-DPA1*02:03-DPB1*39:01', 'HLA-DPA1*02:03-DPB1*40:01',
         'HLA-DPA1*02:03-DPB1*41:01', 'HLA-DPA1*02:03-DPB1*44:01', 'HLA-DPA1*02:03-DPB1*45:01', 'HLA-DPA1*02:03-DPB1*46:01',
         'HLA-DPA1*02:03-DPB1*47:01', 'HLA-DPA1*02:03-DPB1*48:01',
         'HLA-DPA1*02:03-DPB1*49:01', 'HLA-DPA1*02:03-DPB1*50:01', 'HLA-DPA1*02:03-DPB1*51:01', 'HLA-DPA1*02:03-DPB1*52:01',
         'HLA-DPA1*02:03-DPB1*53:01', 'HLA-DPA1*02:03-DPB1*54:01',
         'HLA-DPA1*02:03-DPB1*55:01', 'HLA-DPA1*02:03-DPB1*56:01', 'HLA-DPA1*02:03-DPB1*58:01', 'HLA-DPA1*02:03-DPB1*59:01',
         'HLA-DPA1*02:03-DPB1*60:01', 'HLA-DPA1*02:03-DPB1*62:01',
         'HLA-DPA1*02:03-DPB1*63:01', 'HLA-DPA1*02:03-DPB1*65:01', 'HLA-DPA1*02:03-DPB1*66:01', 'HLA-DPA1*02:03-DPB1*67:01',
         'HLA-DPA1*02:03-DPB1*68:01', 'HLA-DPA1*02:03-DPB1*69:01',
         'HLA-DPA1*02:03-DPB1*70:01', 'HLA-DPA1*02:03-DPB1*71:01', 'HLA-DPA1*02:03-DPB1*72:01', 'HLA-DPA1*02:03-DPB1*73:01',
         'HLA-DPA1*02:03-DPB1*74:01', 'HLA-DPA1*02:03-DPB1*75:01',
         'HLA-DPA1*02:03-DPB1*76:01', 'HLA-DPA1*02:03-DPB1*77:01', 'HLA-DPA1*02:03-DPB1*78:01', 'HLA-DPA1*02:03-DPB1*79:01',
         'HLA-DPA1*02:03-DPB1*80:01', 'HLA-DPA1*02:03-DPB1*81:01',
         'HLA-DPA1*02:03-DPB1*82:01', 'HLA-DPA1*02:03-DPB1*83:01', 'HLA-DPA1*02:03-DPB1*84:01', 'HLA-DPA1*02:03-DPB1*85:01',
         'HLA-DPA1*02:03-DPB1*86:01', 'HLA-DPA1*02:03-DPB1*87:01',
         'HLA-DPA1*02:03-DPB1*88:01', 'HLA-DPA1*02:03-DPB1*89:01', 'HLA-DPA1*02:03-DPB1*90:01', 'HLA-DPA1*02:03-DPB1*91:01',
         'HLA-DPA1*02:03-DPB1*92:01', 'HLA-DPA1*02:03-DPB1*93:01',
         'HLA-DPA1*02:03-DPB1*94:01', 'HLA-DPA1*02:03-DPB1*95:01', 'HLA-DPA1*02:03-DPB1*96:01', 'HLA-DPA1*02:03-DPB1*97:01',
         'HLA-DPA1*02:03-DPB1*98:01', 'HLA-DPA1*02:03-DPB1*99:01',
         'HLA-DPA1*02:04-DPB1*01:01', 'HLA-DPA1*02:04-DPB1*02:01', 'HLA-DPA1*02:04-DPB1*02:02', 'HLA-DPA1*02:04-DPB1*03:01',
         'HLA-DPA1*02:04-DPB1*04:01', 'HLA-DPA1*02:04-DPB1*04:02',
         'HLA-DPA1*02:04-DPB1*05:01', 'HLA-DPA1*02:04-DPB1*06:01', 'HLA-DPA1*02:04-DPB1*08:01', 'HLA-DPA1*02:04-DPB1*09:01',
         'HLA-DPA1*02:04-DPB1*10:001', 'HLA-DPA1*02:04-DPB1*10:01',
         'HLA-DPA1*02:04-DPB1*10:101', 'HLA-DPA1*02:04-DPB1*10:201', 'HLA-DPA1*02:04-DPB1*10:301', 'HLA-DPA1*02:04-DPB1*10:401',
         'HLA-DPA1*02:04-DPB1*10:501', 'HLA-DPA1*02:04-DPB1*10:601',
         'HLA-DPA1*02:04-DPB1*10:701', 'HLA-DPA1*02:04-DPB1*10:801', 'HLA-DPA1*02:04-DPB1*10:901', 'HLA-DPA1*02:04-DPB1*11:001',
         'HLA-DPA1*02:04-DPB1*11:01', 'HLA-DPA1*02:04-DPB1*11:101',
         'HLA-DPA1*02:04-DPB1*11:201', 'HLA-DPA1*02:04-DPB1*11:301', 'HLA-DPA1*02:04-DPB1*11:401', 'HLA-DPA1*02:04-DPB1*11:501',
         'HLA-DPA1*02:04-DPB1*11:601', 'HLA-DPA1*02:04-DPB1*11:701',
         'HLA-DPA1*02:04-DPB1*11:801', 'HLA-DPA1*02:04-DPB1*11:901', 'HLA-DPA1*02:04-DPB1*12:101', 'HLA-DPA1*02:04-DPB1*12:201',
         'HLA-DPA1*02:04-DPB1*12:301', 'HLA-DPA1*02:04-DPB1*12:401',
         'HLA-DPA1*02:04-DPB1*12:501', 'HLA-DPA1*02:04-DPB1*12:601', 'HLA-DPA1*02:04-DPB1*12:701', 'HLA-DPA1*02:04-DPB1*12:801',
         'HLA-DPA1*02:04-DPB1*12:901', 'HLA-DPA1*02:04-DPB1*13:001',
         'HLA-DPA1*02:04-DPB1*13:01', 'HLA-DPA1*02:04-DPB1*13:101', 'HLA-DPA1*02:04-DPB1*13:201', 'HLA-DPA1*02:04-DPB1*13:301',
         'HLA-DPA1*02:04-DPB1*13:401', 'HLA-DPA1*02:04-DPB1*14:01',
         'HLA-DPA1*02:04-DPB1*15:01', 'HLA-DPA1*02:04-DPB1*16:01', 'HLA-DPA1*02:04-DPB1*17:01', 'HLA-DPA1*02:04-DPB1*18:01',
         'HLA-DPA1*02:04-DPB1*19:01', 'HLA-DPA1*02:04-DPB1*20:01',
         'HLA-DPA1*02:04-DPB1*21:01', 'HLA-DPA1*02:04-DPB1*22:01', 'HLA-DPA1*02:04-DPB1*23:01', 'HLA-DPA1*02:04-DPB1*24:01',
         'HLA-DPA1*02:04-DPB1*25:01', 'HLA-DPA1*02:04-DPB1*26:01',
         'HLA-DPA1*02:04-DPB1*27:01', 'HLA-DPA1*02:04-DPB1*28:01', 'HLA-DPA1*02:04-DPB1*29:01', 'HLA-DPA1*02:04-DPB1*30:01',
         'HLA-DPA1*02:04-DPB1*31:01', 'HLA-DPA1*02:04-DPB1*32:01',
         'HLA-DPA1*02:04-DPB1*33:01', 'HLA-DPA1*02:04-DPB1*34:01', 'HLA-DPA1*02:04-DPB1*35:01', 'HLA-DPA1*02:04-DPB1*36:01',
         'HLA-DPA1*02:04-DPB1*37:01', 'HLA-DPA1*02:04-DPB1*38:01',
         'HLA-DPA1*02:04-DPB1*39:01', 'HLA-DPA1*02:04-DPB1*40:01', 'HLA-DPA1*02:04-DPB1*41:01', 'HLA-DPA1*02:04-DPB1*44:01',
         'HLA-DPA1*02:04-DPB1*45:01', 'HLA-DPA1*02:04-DPB1*46:01',
         'HLA-DPA1*02:04-DPB1*47:01', 'HLA-DPA1*02:04-DPB1*48:01', 'HLA-DPA1*02:04-DPB1*49:01', 'HLA-DPA1*02:04-DPB1*50:01',
         'HLA-DPA1*02:04-DPB1*51:01', 'HLA-DPA1*02:04-DPB1*52:01',
         'HLA-DPA1*02:04-DPB1*53:01', 'HLA-DPA1*02:04-DPB1*54:01', 'HLA-DPA1*02:04-DPB1*55:01', 'HLA-DPA1*02:04-DPB1*56:01',
         'HLA-DPA1*02:04-DPB1*58:01', 'HLA-DPA1*02:04-DPB1*59:01',
         'HLA-DPA1*02:04-DPB1*60:01', 'HLA-DPA1*02:04-DPB1*62:01', 'HLA-DPA1*02:04-DPB1*63:01', 'HLA-DPA1*02:04-DPB1*65:01',
         'HLA-DPA1*02:04-DPB1*66:01', 'HLA-DPA1*02:04-DPB1*67:01',
         'HLA-DPA1*02:04-DPB1*68:01', 'HLA-DPA1*02:04-DPB1*69:01', 'HLA-DPA1*02:04-DPB1*70:01', 'HLA-DPA1*02:04-DPB1*71:01',
         'HLA-DPA1*02:04-DPB1*72:01', 'HLA-DPA1*02:04-DPB1*73:01',
         'HLA-DPA1*02:04-DPB1*74:01', 'HLA-DPA1*02:04-DPB1*75:01', 'HLA-DPA1*02:04-DPB1*76:01', 'HLA-DPA1*02:04-DPB1*77:01',
         'HLA-DPA1*02:04-DPB1*78:01', 'HLA-DPA1*02:04-DPB1*79:01',
         'HLA-DPA1*02:04-DPB1*80:01', 'HLA-DPA1*02:04-DPB1*81:01', 'HLA-DPA1*02:04-DPB1*82:01', 'HLA-DPA1*02:04-DPB1*83:01',
         'HLA-DPA1*02:04-DPB1*84:01', 'HLA-DPA1*02:04-DPB1*85:01',
         'HLA-DPA1*02:04-DPB1*86:01', 'HLA-DPA1*02:04-DPB1*87:01', 'HLA-DPA1*02:04-DPB1*88:01', 'HLA-DPA1*02:04-DPB1*89:01',
         'HLA-DPA1*02:04-DPB1*90:01', 'HLA-DPA1*02:04-DPB1*91:01',
         'HLA-DPA1*02:04-DPB1*92:01', 'HLA-DPA1*02:04-DPB1*93:01', 'HLA-DPA1*02:04-DPB1*94:01', 'HLA-DPA1*02:04-DPB1*95:01',
         'HLA-DPA1*02:04-DPB1*96:01', 'HLA-DPA1*02:04-DPB1*97:01',
         'HLA-DPA1*02:04-DPB1*98:01', 'HLA-DPA1*02:04-DPB1*99:01', 'HLA-DPA1*03:01-DPB1*01:01', 'HLA-DPA1*03:01-DPB1*02:01',
         'HLA-DPA1*03:01-DPB1*02:02', 'HLA-DPA1*03:01-DPB1*03:01',
         'HLA-DPA1*03:01-DPB1*04:01', 'HLA-DPA1*03:01-DPB1*04:02', 'HLA-DPA1*03:01-DPB1*05:01', 'HLA-DPA1*03:01-DPB1*06:01',
         'HLA-DPA1*03:01-DPB1*08:01', 'HLA-DPA1*03:01-DPB1*09:01',
         'HLA-DPA1*03:01-DPB1*10:001', 'HLA-DPA1*03:01-DPB1*10:01', 'HLA-DPA1*03:01-DPB1*10:101', 'HLA-DPA1*03:01-DPB1*10:201',
         'HLA-DPA1*03:01-DPB1*10:301', 'HLA-DPA1*03:01-DPB1*10:401',
         'HLA-DPA1*03:01-DPB1*10:501', 'HLA-DPA1*03:01-DPB1*10:601', 'HLA-DPA1*03:01-DPB1*10:701', 'HLA-DPA1*03:01-DPB1*10:801',
         'HLA-DPA1*03:01-DPB1*10:901', 'HLA-DPA1*03:01-DPB1*11:001',
         'HLA-DPA1*03:01-DPB1*11:01', 'HLA-DPA1*03:01-DPB1*11:101', 'HLA-DPA1*03:01-DPB1*11:201', 'HLA-DPA1*03:01-DPB1*11:301',
         'HLA-DPA1*03:01-DPB1*11:401', 'HLA-DPA1*03:01-DPB1*11:501',
         'HLA-DPA1*03:01-DPB1*11:601', 'HLA-DPA1*03:01-DPB1*11:701', 'HLA-DPA1*03:01-DPB1*11:801', 'HLA-DPA1*03:01-DPB1*11:901',
         'HLA-DPA1*03:01-DPB1*12:101', 'HLA-DPA1*03:01-DPB1*12:201',
         'HLA-DPA1*03:01-DPB1*12:301', 'HLA-DPA1*03:01-DPB1*12:401', 'HLA-DPA1*03:01-DPB1*12:501', 'HLA-DPA1*03:01-DPB1*12:601',
         'HLA-DPA1*03:01-DPB1*12:701', 'HLA-DPA1*03:01-DPB1*12:801',
         'HLA-DPA1*03:01-DPB1*12:901', 'HLA-DPA1*03:01-DPB1*13:001', 'HLA-DPA1*03:01-DPB1*13:01', 'HLA-DPA1*03:01-DPB1*13:101',
         'HLA-DPA1*03:01-DPB1*13:201', 'HLA-DPA1*03:01-DPB1*13:301',
         'HLA-DPA1*03:01-DPB1*13:401', 'HLA-DPA1*03:01-DPB1*14:01', 'HLA-DPA1*03:01-DPB1*15:01', 'HLA-DPA1*03:01-DPB1*16:01',
         'HLA-DPA1*03:01-DPB1*17:01', 'HLA-DPA1*03:01-DPB1*18:01',
         'HLA-DPA1*03:01-DPB1*19:01', 'HLA-DPA1*03:01-DPB1*20:01', 'HLA-DPA1*03:01-DPB1*21:01', 'HLA-DPA1*03:01-DPB1*22:01',
         'HLA-DPA1*03:01-DPB1*23:01', 'HLA-DPA1*03:01-DPB1*24:01',
         'HLA-DPA1*03:01-DPB1*25:01', 'HLA-DPA1*03:01-DPB1*26:01', 'HLA-DPA1*03:01-DPB1*27:01', 'HLA-DPA1*03:01-DPB1*28:01',
         'HLA-DPA1*03:01-DPB1*29:01', 'HLA-DPA1*03:01-DPB1*30:01',
         'HLA-DPA1*03:01-DPB1*31:01', 'HLA-DPA1*03:01-DPB1*32:01', 'HLA-DPA1*03:01-DPB1*33:01', 'HLA-DPA1*03:01-DPB1*34:01',
         'HLA-DPA1*03:01-DPB1*35:01', 'HLA-DPA1*03:01-DPB1*36:01',
         'HLA-DPA1*03:01-DPB1*37:01', 'HLA-DPA1*03:01-DPB1*38:01', 'HLA-DPA1*03:01-DPB1*39:01', 'HLA-DPA1*03:01-DPB1*40:01',
         'HLA-DPA1*03:01-DPB1*41:01', 'HLA-DPA1*03:01-DPB1*44:01',
         'HLA-DPA1*03:01-DPB1*45:01', 'HLA-DPA1*03:01-DPB1*46:01', 'HLA-DPA1*03:01-DPB1*47:01', 'HLA-DPA1*03:01-DPB1*48:01',
         'HLA-DPA1*03:01-DPB1*49:01', 'HLA-DPA1*03:01-DPB1*50:01',
         'HLA-DPA1*03:01-DPB1*51:01', 'HLA-DPA1*03:01-DPB1*52:01', 'HLA-DPA1*03:01-DPB1*53:01', 'HLA-DPA1*03:01-DPB1*54:01',
         'HLA-DPA1*03:01-DPB1*55:01', 'HLA-DPA1*03:01-DPB1*56:01',
         'HLA-DPA1*03:01-DPB1*58:01', 'HLA-DPA1*03:01-DPB1*59:01', 'HLA-DPA1*03:01-DPB1*60:01', 'HLA-DPA1*03:01-DPB1*62:01',
         'HLA-DPA1*03:01-DPB1*63:01', 'HLA-DPA1*03:01-DPB1*65:01',
         'HLA-DPA1*03:01-DPB1*66:01', 'HLA-DPA1*03:01-DPB1*67:01', 'HLA-DPA1*03:01-DPB1*68:01', 'HLA-DPA1*03:01-DPB1*69:01',
         'HLA-DPA1*03:01-DPB1*70:01', 'HLA-DPA1*03:01-DPB1*71:01',
         'HLA-DPA1*03:01-DPB1*72:01', 'HLA-DPA1*03:01-DPB1*73:01', 'HLA-DPA1*03:01-DPB1*74:01', 'HLA-DPA1*03:01-DPB1*75:01',
         'HLA-DPA1*03:01-DPB1*76:01', 'HLA-DPA1*03:01-DPB1*77:01',
         'HLA-DPA1*03:01-DPB1*78:01', 'HLA-DPA1*03:01-DPB1*79:01', 'HLA-DPA1*03:01-DPB1*80:01', 'HLA-DPA1*03:01-DPB1*81:01',
         'HLA-DPA1*03:01-DPB1*82:01', 'HLA-DPA1*03:01-DPB1*83:01',
         'HLA-DPA1*03:01-DPB1*84:01', 'HLA-DPA1*03:01-DPB1*85:01', 'HLA-DPA1*03:01-DPB1*86:01', 'HLA-DPA1*03:01-DPB1*87:01',
         'HLA-DPA1*03:01-DPB1*88:01', 'HLA-DPA1*03:01-DPB1*89:01',
         'HLA-DPA1*03:01-DPB1*90:01', 'HLA-DPA1*03:01-DPB1*91:01', 'HLA-DPA1*03:01-DPB1*92:01', 'HLA-DPA1*03:01-DPB1*93:01',
         'HLA-DPA1*03:01-DPB1*94:01', 'HLA-DPA1*03:01-DPB1*95:01',
         'HLA-DPA1*03:01-DPB1*96:01', 'HLA-DPA1*03:01-DPB1*97:01', 'HLA-DPA1*03:01-DPB1*98:01', 'HLA-DPA1*03:01-DPB1*99:01',
         'HLA-DPA1*03:02-DPB1*01:01', 'HLA-DPA1*03:02-DPB1*02:01',
         'HLA-DPA1*03:02-DPB1*02:02', 'HLA-DPA1*03:02-DPB1*03:01', 'HLA-DPA1*03:02-DPB1*04:01', 'HLA-DPA1*03:02-DPB1*04:02',
         'HLA-DPA1*03:02-DPB1*05:01', 'HLA-DPA1*03:02-DPB1*06:01',
         'HLA-DPA1*03:02-DPB1*08:01', 'HLA-DPA1*03:02-DPB1*09:01', 'HLA-DPA1*03:02-DPB1*10:001', 'HLA-DPA1*03:02-DPB1*10:01',
         'HLA-DPA1*03:02-DPB1*10:101', 'HLA-DPA1*03:02-DPB1*10:201',
         'HLA-DPA1*03:02-DPB1*10:301', 'HLA-DPA1*03:02-DPB1*10:401', 'HLA-DPA1*03:02-DPB1*10:501', 'HLA-DPA1*03:02-DPB1*10:601',
         'HLA-DPA1*03:02-DPB1*10:701', 'HLA-DPA1*03:02-DPB1*10:801',
         'HLA-DPA1*03:02-DPB1*10:901', 'HLA-DPA1*03:02-DPB1*11:001', 'HLA-DPA1*03:02-DPB1*11:01', 'HLA-DPA1*03:02-DPB1*11:101',
         'HLA-DPA1*03:02-DPB1*11:201', 'HLA-DPA1*03:02-DPB1*11:301',
         'HLA-DPA1*03:02-DPB1*11:401', 'HLA-DPA1*03:02-DPB1*11:501', 'HLA-DPA1*03:02-DPB1*11:601', 'HLA-DPA1*03:02-DPB1*11:701',
         'HLA-DPA1*03:02-DPB1*11:801', 'HLA-DPA1*03:02-DPB1*11:901',
         'HLA-DPA1*03:02-DPB1*12:101', 'HLA-DPA1*03:02-DPB1*12:201', 'HLA-DPA1*03:02-DPB1*12:301', 'HLA-DPA1*03:02-DPB1*12:401',
         'HLA-DPA1*03:02-DPB1*12:501', 'HLA-DPA1*03:02-DPB1*12:601',
         'HLA-DPA1*03:02-DPB1*12:701', 'HLA-DPA1*03:02-DPB1*12:801', 'HLA-DPA1*03:02-DPB1*12:901', 'HLA-DPA1*03:02-DPB1*13:001',
         'HLA-DPA1*03:02-DPB1*13:01', 'HLA-DPA1*03:02-DPB1*13:101',
         'HLA-DPA1*03:02-DPB1*13:201', 'HLA-DPA1*03:02-DPB1*13:301', 'HLA-DPA1*03:02-DPB1*13:401', 'HLA-DPA1*03:02-DPB1*14:01',
         'HLA-DPA1*03:02-DPB1*15:01', 'HLA-DPA1*03:02-DPB1*16:01',
         'HLA-DPA1*03:02-DPB1*17:01', 'HLA-DPA1*03:02-DPB1*18:01', 'HLA-DPA1*03:02-DPB1*19:01', 'HLA-DPA1*03:02-DPB1*20:01',
         'HLA-DPA1*03:02-DPB1*21:01', 'HLA-DPA1*03:02-DPB1*22:01',
         'HLA-DPA1*03:02-DPB1*23:01', 'HLA-DPA1*03:02-DPB1*24:01', 'HLA-DPA1*03:02-DPB1*25:01', 'HLA-DPA1*03:02-DPB1*26:01',
         'HLA-DPA1*03:02-DPB1*27:01', 'HLA-DPA1*03:02-DPB1*28:01',
         'HLA-DPA1*03:02-DPB1*29:01', 'HLA-DPA1*03:02-DPB1*30:01', 'HLA-DPA1*03:02-DPB1*31:01', 'HLA-DPA1*03:02-DPB1*32:01',
         'HLA-DPA1*03:02-DPB1*33:01', 'HLA-DPA1*03:02-DPB1*34:01',
         'HLA-DPA1*03:02-DPB1*35:01', 'HLA-DPA1*03:02-DPB1*36:01', 'HLA-DPA1*03:02-DPB1*37:01', 'HLA-DPA1*03:02-DPB1*38:01',
         'HLA-DPA1*03:02-DPB1*39:01', 'HLA-DPA1*03:02-DPB1*40:01',
         'HLA-DPA1*03:02-DPB1*41:01', 'HLA-DPA1*03:02-DPB1*44:01', 'HLA-DPA1*03:02-DPB1*45:01', 'HLA-DPA1*03:02-DPB1*46:01',
         'HLA-DPA1*03:02-DPB1*47:01', 'HLA-DPA1*03:02-DPB1*48:01',
         'HLA-DPA1*03:02-DPB1*49:01', 'HLA-DPA1*03:02-DPB1*50:01', 'HLA-DPA1*03:02-DPB1*51:01', 'HLA-DPA1*03:02-DPB1*52:01',
         'HLA-DPA1*03:02-DPB1*53:01', 'HLA-DPA1*03:02-DPB1*54:01',
         'HLA-DPA1*03:02-DPB1*55:01', 'HLA-DPA1*03:02-DPB1*56:01', 'HLA-DPA1*03:02-DPB1*58:01', 'HLA-DPA1*03:02-DPB1*59:01',
         'HLA-DPA1*03:02-DPB1*60:01', 'HLA-DPA1*03:02-DPB1*62:01',
         'HLA-DPA1*03:02-DPB1*63:01', 'HLA-DPA1*03:02-DPB1*65:01', 'HLA-DPA1*03:02-DPB1*66:01', 'HLA-DPA1*03:02-DPB1*67:01',
         'HLA-DPA1*03:02-DPB1*68:01', 'HLA-DPA1*03:02-DPB1*69:01',
         'HLA-DPA1*03:02-DPB1*70:01', 'HLA-DPA1*03:02-DPB1*71:01', 'HLA-DPA1*03:02-DPB1*72:01', 'HLA-DPA1*03:02-DPB1*73:01',
         'HLA-DPA1*03:02-DPB1*74:01', 'HLA-DPA1*03:02-DPB1*75:01',
         'HLA-DPA1*03:02-DPB1*76:01', 'HLA-DPA1*03:02-DPB1*77:01', 'HLA-DPA1*03:02-DPB1*78:01', 'HLA-DPA1*03:02-DPB1*79:01',
         'HLA-DPA1*03:02-DPB1*80:01', 'HLA-DPA1*03:02-DPB1*81:01',
         'HLA-DPA1*03:02-DPB1*82:01', 'HLA-DPA1*03:02-DPB1*83:01', 'HLA-DPA1*03:02-DPB1*84:01', 'HLA-DPA1*03:02-DPB1*85:01',
         'HLA-DPA1*03:02-DPB1*86:01', 'HLA-DPA1*03:02-DPB1*87:01',
         'HLA-DPA1*03:02-DPB1*88:01', 'HLA-DPA1*03:02-DPB1*89:01', 'HLA-DPA1*03:02-DPB1*90:01', 'HLA-DPA1*03:02-DPB1*91:01',
         'HLA-DPA1*03:02-DPB1*92:01', 'HLA-DPA1*03:02-DPB1*93:01',
         'HLA-DPA1*03:02-DPB1*94:01', 'HLA-DPA1*03:02-DPB1*95:01', 'HLA-DPA1*03:02-DPB1*96:01', 'HLA-DPA1*03:02-DPB1*97:01',
         'HLA-DPA1*03:02-DPB1*98:01', 'HLA-DPA1*03:02-DPB1*99:01',
         'HLA-DPA1*03:03-DPB1*01:01', 'HLA-DPA1*03:03-DPB1*02:01', 'HLA-DPA1*03:03-DPB1*02:02', 'HLA-DPA1*03:03-DPB1*03:01',
         'HLA-DPA1*03:03-DPB1*04:01', 'HLA-DPA1*03:03-DPB1*04:02',
         'HLA-DPA1*03:03-DPB1*05:01', 'HLA-DPA1*03:03-DPB1*06:01', 'HLA-DPA1*03:03-DPB1*08:01', 'HLA-DPA1*03:03-DPB1*09:01',
         'HLA-DPA1*03:03-DPB1*10:001', 'HLA-DPA1*03:03-DPB1*10:01',
         'HLA-DPA1*03:03-DPB1*10:101', 'HLA-DPA1*03:03-DPB1*10:201', 'HLA-DPA1*03:03-DPB1*10:301', 'HLA-DPA1*03:03-DPB1*10:401',
         'HLA-DPA1*03:03-DPB1*10:501', 'HLA-DPA1*03:03-DPB1*10:601',
         'HLA-DPA1*03:03-DPB1*10:701', 'HLA-DPA1*03:03-DPB1*10:801', 'HLA-DPA1*03:03-DPB1*10:901', 'HLA-DPA1*03:03-DPB1*11:001',
         'HLA-DPA1*03:03-DPB1*11:01', 'HLA-DPA1*03:03-DPB1*11:101',
         'HLA-DPA1*03:03-DPB1*11:201', 'HLA-DPA1*03:03-DPB1*11:301', 'HLA-DPA1*03:03-DPB1*11:401', 'HLA-DPA1*03:03-DPB1*11:501',
         'HLA-DPA1*03:03-DPB1*11:601', 'HLA-DPA1*03:03-DPB1*11:701',
         'HLA-DPA1*03:03-DPB1*11:801', 'HLA-DPA1*03:03-DPB1*11:901', 'HLA-DPA1*03:03-DPB1*12:101', 'HLA-DPA1*03:03-DPB1*12:201',
         'HLA-DPA1*03:03-DPB1*12:301', 'HLA-DPA1*03:03-DPB1*12:401',
         'HLA-DPA1*03:03-DPB1*12:501', 'HLA-DPA1*03:03-DPB1*12:601', 'HLA-DPA1*03:03-DPB1*12:701', 'HLA-DPA1*03:03-DPB1*12:801',
         'HLA-DPA1*03:03-DPB1*12:901', 'HLA-DPA1*03:03-DPB1*13:001',
         'HLA-DPA1*03:03-DPB1*13:01', 'HLA-DPA1*03:03-DPB1*13:101', 'HLA-DPA1*03:03-DPB1*13:201', 'HLA-DPA1*03:03-DPB1*13:301',
         'HLA-DPA1*03:03-DPB1*13:401', 'HLA-DPA1*03:03-DPB1*14:01',
         'HLA-DPA1*03:03-DPB1*15:01', 'HLA-DPA1*03:03-DPB1*16:01', 'HLA-DPA1*03:03-DPB1*17:01', 'HLA-DPA1*03:03-DPB1*18:01',
         'HLA-DPA1*03:03-DPB1*19:01', 'HLA-DPA1*03:03-DPB1*20:01',
         'HLA-DPA1*03:03-DPB1*21:01', 'HLA-DPA1*03:03-DPB1*22:01', 'HLA-DPA1*03:03-DPB1*23:01', 'HLA-DPA1*03:03-DPB1*24:01',
         'HLA-DPA1*03:03-DPB1*25:01', 'HLA-DPA1*03:03-DPB1*26:01',
         'HLA-DPA1*03:03-DPB1*27:01', 'HLA-DPA1*03:03-DPB1*28:01', 'HLA-DPA1*03:03-DPB1*29:01', 'HLA-DPA1*03:03-DPB1*30:01',
         'HLA-DPA1*03:03-DPB1*31:01', 'HLA-DPA1*03:03-DPB1*32:01',
         'HLA-DPA1*03:03-DPB1*33:01', 'HLA-DPA1*03:03-DPB1*34:01', 'HLA-DPA1*03:03-DPB1*35:01', 'HLA-DPA1*03:03-DPB1*36:01',
         'HLA-DPA1*03:03-DPB1*37:01', 'HLA-DPA1*03:03-DPB1*38:01',
         'HLA-DPA1*03:03-DPB1*39:01', 'HLA-DPA1*03:03-DPB1*40:01', 'HLA-DPA1*03:03-DPB1*41:01', 'HLA-DPA1*03:03-DPB1*44:01',
         'HLA-DPA1*03:03-DPB1*45:01', 'HLA-DPA1*03:03-DPB1*46:01',
         'HLA-DPA1*03:03-DPB1*47:01', 'HLA-DPA1*03:03-DPB1*48:01', 'HLA-DPA1*03:03-DPB1*49:01', 'HLA-DPA1*03:03-DPB1*50:01',
         'HLA-DPA1*03:03-DPB1*51:01', 'HLA-DPA1*03:03-DPB1*52:01',
         'HLA-DPA1*03:03-DPB1*53:01', 'HLA-DPA1*03:03-DPB1*54:01', 'HLA-DPA1*03:03-DPB1*55:01', 'HLA-DPA1*03:03-DPB1*56:01',
         'HLA-DPA1*03:03-DPB1*58:01', 'HLA-DPA1*03:03-DPB1*59:01',
         'HLA-DPA1*03:03-DPB1*60:01', 'HLA-DPA1*03:03-DPB1*62:01', 'HLA-DPA1*03:03-DPB1*63:01', 'HLA-DPA1*03:03-DPB1*65:01',
         'HLA-DPA1*03:03-DPB1*66:01', 'HLA-DPA1*03:03-DPB1*67:01',
         'HLA-DPA1*03:03-DPB1*68:01', 'HLA-DPA1*03:03-DPB1*69:01', 'HLA-DPA1*03:03-DPB1*70:01', 'HLA-DPA1*03:03-DPB1*71:01',
         'HLA-DPA1*03:03-DPB1*72:01', 'HLA-DPA1*03:03-DPB1*73:01',
         'HLA-DPA1*03:03-DPB1*74:01', 'HLA-DPA1*03:03-DPB1*75:01', 'HLA-DPA1*03:03-DPB1*76:01', 'HLA-DPA1*03:03-DPB1*77:01',
         'HLA-DPA1*03:03-DPB1*78:01', 'HLA-DPA1*03:03-DPB1*79:01',
         'HLA-DPA1*03:03-DPB1*80:01', 'HLA-DPA1*03:03-DPB1*81:01', 'HLA-DPA1*03:03-DPB1*82:01', 'HLA-DPA1*03:03-DPB1*83:01',
         'HLA-DPA1*03:03-DPB1*84:01', 'HLA-DPA1*03:03-DPB1*85:01',
         'HLA-DPA1*03:03-DPB1*86:01', 'HLA-DPA1*03:03-DPB1*87:01', 'HLA-DPA1*03:03-DPB1*88:01', 'HLA-DPA1*03:03-DPB1*89:01',
         'HLA-DPA1*03:03-DPB1*90:01', 'HLA-DPA1*03:03-DPB1*91:01',
         'HLA-DPA1*03:03-DPB1*92:01', 'HLA-DPA1*03:03-DPB1*93:01', 'HLA-DPA1*03:03-DPB1*94:01', 'HLA-DPA1*03:03-DPB1*95:01',
         'HLA-DPA1*03:03-DPB1*96:01', 'HLA-DPA1*03:03-DPB1*97:01',
         'HLA-DPA1*03:03-DPB1*98:01', 'HLA-DPA1*03:03-DPB1*99:01', 'HLA-DPA1*04:01-DPB1*01:01', 'HLA-DPA1*04:01-DPB1*02:01',
         'HLA-DPA1*04:01-DPB1*02:02', 'HLA-DPA1*04:01-DPB1*03:01',
         'HLA-DPA1*04:01-DPB1*04:01', 'HLA-DPA1*04:01-DPB1*04:02', 'HLA-DPA1*04:01-DPB1*05:01', 'HLA-DPA1*04:01-DPB1*06:01',
         'HLA-DPA1*04:01-DPB1*08:01', 'HLA-DPA1*04:01-DPB1*09:01',
         'HLA-DPA1*04:01-DPB1*10:001', 'HLA-DPA1*04:01-DPB1*10:01', 'HLA-DPA1*04:01-DPB1*10:101', 'HLA-DPA1*04:01-DPB1*10:201',
         'HLA-DPA1*04:01-DPB1*10:301', 'HLA-DPA1*04:01-DPB1*10:401',
         'HLA-DPA1*04:01-DPB1*10:501', 'HLA-DPA1*04:01-DPB1*10:601', 'HLA-DPA1*04:01-DPB1*10:701', 'HLA-DPA1*04:01-DPB1*10:801',
         'HLA-DPA1*04:01-DPB1*10:901', 'HLA-DPA1*04:01-DPB1*11:001',
         'HLA-DPA1*04:01-DPB1*11:01', 'HLA-DPA1*04:01-DPB1*11:101', 'HLA-DPA1*04:01-DPB1*11:201', 'HLA-DPA1*04:01-DPB1*11:301',
         'HLA-DPA1*04:01-DPB1*11:401', 'HLA-DPA1*04:01-DPB1*11:501',
         'HLA-DPA1*04:01-DPB1*11:601', 'HLA-DPA1*04:01-DPB1*11:701', 'HLA-DPA1*04:01-DPB1*11:801', 'HLA-DPA1*04:01-DPB1*11:901',
         'HLA-DPA1*04:01-DPB1*12:101', 'HLA-DPA1*04:01-DPB1*12:201',
         'HLA-DPA1*04:01-DPB1*12:301', 'HLA-DPA1*04:01-DPB1*12:401', 'HLA-DPA1*04:01-DPB1*12:501', 'HLA-DPA1*04:01-DPB1*12:601',
         'HLA-DPA1*04:01-DPB1*12:701', 'HLA-DPA1*04:01-DPB1*12:801',
         'HLA-DPA1*04:01-DPB1*12:901', 'HLA-DPA1*04:01-DPB1*13:001', 'HLA-DPA1*04:01-DPB1*13:01', 'HLA-DPA1*04:01-DPB1*13:101',
         'HLA-DPA1*04:01-DPB1*13:201', 'HLA-DPA1*04:01-DPB1*13:301',
         'HLA-DPA1*04:01-DPB1*13:401', 'HLA-DPA1*04:01-DPB1*14:01', 'HLA-DPA1*04:01-DPB1*15:01', 'HLA-DPA1*04:01-DPB1*16:01',
         'HLA-DPA1*04:01-DPB1*17:01', 'HLA-DPA1*04:01-DPB1*18:01',
         'HLA-DPA1*04:01-DPB1*19:01', 'HLA-DPA1*04:01-DPB1*20:01', 'HLA-DPA1*04:01-DPB1*21:01', 'HLA-DPA1*04:01-DPB1*22:01',
         'HLA-DPA1*04:01-DPB1*23:01', 'HLA-DPA1*04:01-DPB1*24:01',
         'HLA-DPA1*04:01-DPB1*25:01', 'HLA-DPA1*04:01-DPB1*26:01', 'HLA-DPA1*04:01-DPB1*27:01', 'HLA-DPA1*04:01-DPB1*28:01',
         'HLA-DPA1*04:01-DPB1*29:01', 'HLA-DPA1*04:01-DPB1*30:01',
         'HLA-DPA1*04:01-DPB1*31:01', 'HLA-DPA1*04:01-DPB1*32:01', 'HLA-DPA1*04:01-DPB1*33:01', 'HLA-DPA1*04:01-DPB1*34:01',
         'HLA-DPA1*04:01-DPB1*35:01', 'HLA-DPA1*04:01-DPB1*36:01',
         'HLA-DPA1*04:01-DPB1*37:01', 'HLA-DPA1*04:01-DPB1*38:01', 'HLA-DPA1*04:01-DPB1*39:01', 'HLA-DPA1*04:01-DPB1*40:01',
         'HLA-DPA1*04:01-DPB1*41:01', 'HLA-DPA1*04:01-DPB1*44:01',
         'HLA-DPA1*04:01-DPB1*45:01', 'HLA-DPA1*04:01-DPB1*46:01', 'HLA-DPA1*04:01-DPB1*47:01', 'HLA-DPA1*04:01-DPB1*48:01',
         'HLA-DPA1*04:01-DPB1*49:01', 'HLA-DPA1*04:01-DPB1*50:01',
         'HLA-DPA1*04:01-DPB1*51:01', 'HLA-DPA1*04:01-DPB1*52:01', 'HLA-DPA1*04:01-DPB1*53:01', 'HLA-DPA1*04:01-DPB1*54:01',
         'HLA-DPA1*04:01-DPB1*55:01', 'HLA-DPA1*04:01-DPB1*56:01',
         'HLA-DPA1*04:01-DPB1*58:01', 'HLA-DPA1*04:01-DPB1*59:01', 'HLA-DPA1*04:01-DPB1*60:01', 'HLA-DPA1*04:01-DPB1*62:01',
         'HLA-DPA1*04:01-DPB1*63:01', 'HLA-DPA1*04:01-DPB1*65:01',
         'HLA-DPA1*04:01-DPB1*66:01', 'HLA-DPA1*04:01-DPB1*67:01', 'HLA-DPA1*04:01-DPB1*68:01', 'HLA-DPA1*04:01-DPB1*69:01',
         'HLA-DPA1*04:01-DPB1*70:01', 'HLA-DPA1*04:01-DPB1*71:01',
         'HLA-DPA1*04:01-DPB1*72:01', 'HLA-DPA1*04:01-DPB1*73:01', 'HLA-DPA1*04:01-DPB1*74:01', 'HLA-DPA1*04:01-DPB1*75:01',
         'HLA-DPA1*04:01-DPB1*76:01', 'HLA-DPA1*04:01-DPB1*77:01',
         'HLA-DPA1*04:01-DPB1*78:01', 'HLA-DPA1*04:01-DPB1*79:01', 'HLA-DPA1*04:01-DPB1*80:01', 'HLA-DPA1*04:01-DPB1*81:01',
         'HLA-DPA1*04:01-DPB1*82:01', 'HLA-DPA1*04:01-DPB1*83:01',
         'HLA-DPA1*04:01-DPB1*84:01', 'HLA-DPA1*04:01-DPB1*85:01', 'HLA-DPA1*04:01-DPB1*86:01', 'HLA-DPA1*04:01-DPB1*87:01',
         'HLA-DPA1*04:01-DPB1*88:01', 'HLA-DPA1*04:01-DPB1*89:01',
         'HLA-DPA1*04:01-DPB1*90:01', 'HLA-DPA1*04:01-DPB1*91:01', 'HLA-DPA1*04:01-DPB1*92:01', 'HLA-DPA1*04:01-DPB1*93:01',
         'HLA-DPA1*04:01-DPB1*94:01', 'HLA-DPA1*04:01-DPB1*95:01',
         'HLA-DPA1*04:01-DPB1*96:01', 'HLA-DPA1*04:01-DPB1*97:01', 'HLA-DPA1*04:01-DPB1*98:01', 'HLA-DPA1*04:01-DPB1*99:01',
         'HLA-DQA1*01:01-DQB1*02:01', 'HLA-DQA1*01:01-DQB1*02:02',
         'HLA-DQA1*01:01-DQB1*02:03', 'HLA-DQA1*01:01-DQB1*02:04', 'HLA-DQA1*01:01-DQB1*02:05', 'HLA-DQA1*01:01-DQB1*02:06',
         'HLA-DQA1*01:01-DQB1*03:01', 'HLA-DQA1*01:01-DQB1*03:02',
         'HLA-DQA1*01:01-DQB1*03:03', 'HLA-DQA1*01:01-DQB1*03:04', 'HLA-DQA1*01:01-DQB1*03:05', 'HLA-DQA1*01:01-DQB1*03:06',
         'HLA-DQA1*01:01-DQB1*03:07', 'HLA-DQA1*01:01-DQB1*03:08',
         'HLA-DQA1*01:01-DQB1*03:09', 'HLA-DQA1*01:01-DQB1*03:10', 'HLA-DQA1*01:01-DQB1*03:11', 'HLA-DQA1*01:01-DQB1*03:12',
         'HLA-DQA1*01:01-DQB1*03:13', 'HLA-DQA1*01:01-DQB1*03:14',
         'HLA-DQA1*01:01-DQB1*03:15', 'HLA-DQA1*01:01-DQB1*03:16', 'HLA-DQA1*01:01-DQB1*03:17', 'HLA-DQA1*01:01-DQB1*03:18',
         'HLA-DQA1*01:01-DQB1*03:19', 'HLA-DQA1*01:01-DQB1*03:20',
         'HLA-DQA1*01:01-DQB1*03:21', 'HLA-DQA1*01:01-DQB1*03:22', 'HLA-DQA1*01:01-DQB1*03:23', 'HLA-DQA1*01:01-DQB1*03:24',
         'HLA-DQA1*01:01-DQB1*03:25', 'HLA-DQA1*01:01-DQB1*03:26',
         'HLA-DQA1*01:01-DQB1*03:27', 'HLA-DQA1*01:01-DQB1*03:28', 'HLA-DQA1*01:01-DQB1*03:29', 'HLA-DQA1*01:01-DQB1*03:30',
         'HLA-DQA1*01:01-DQB1*03:31', 'HLA-DQA1*01:01-DQB1*03:32',
         'HLA-DQA1*01:01-DQB1*03:33', 'HLA-DQA1*01:01-DQB1*03:34', 'HLA-DQA1*01:01-DQB1*03:35', 'HLA-DQA1*01:01-DQB1*03:36',
         'HLA-DQA1*01:01-DQB1*03:37', 'HLA-DQA1*01:01-DQB1*03:38',
         'HLA-DQA1*01:01-DQB1*04:01', 'HLA-DQA1*01:01-DQB1*04:02', 'HLA-DQA1*01:01-DQB1*04:03', 'HLA-DQA1*01:01-DQB1*04:04',
         'HLA-DQA1*01:01-DQB1*04:05', 'HLA-DQA1*01:01-DQB1*04:06',
         'HLA-DQA1*01:01-DQB1*04:07', 'HLA-DQA1*01:01-DQB1*04:08', 'HLA-DQA1*01:01-DQB1*05:01', 'HLA-DQA1*01:01-DQB1*05:02',
         'HLA-DQA1*01:01-DQB1*05:03', 'HLA-DQA1*01:01-DQB1*05:05',
         'HLA-DQA1*01:01-DQB1*05:06', 'HLA-DQA1*01:01-DQB1*05:07', 'HLA-DQA1*01:01-DQB1*05:08', 'HLA-DQA1*01:01-DQB1*05:09',
         'HLA-DQA1*01:01-DQB1*05:10', 'HLA-DQA1*01:01-DQB1*05:11',
         'HLA-DQA1*01:01-DQB1*05:12', 'HLA-DQA1*01:01-DQB1*05:13', 'HLA-DQA1*01:01-DQB1*05:14', 'HLA-DQA1*01:01-DQB1*06:01',
         'HLA-DQA1*01:01-DQB1*06:02', 'HLA-DQA1*01:01-DQB1*06:03',
         'HLA-DQA1*01:01-DQB1*06:04', 'HLA-DQA1*01:01-DQB1*06:07', 'HLA-DQA1*01:01-DQB1*06:08', 'HLA-DQA1*01:01-DQB1*06:09',
         'HLA-DQA1*01:01-DQB1*06:10', 'HLA-DQA1*01:01-DQB1*06:11',
         'HLA-DQA1*01:01-DQB1*06:12', 'HLA-DQA1*01:01-DQB1*06:14', 'HLA-DQA1*01:01-DQB1*06:15', 'HLA-DQA1*01:01-DQB1*06:16',
         'HLA-DQA1*01:01-DQB1*06:17', 'HLA-DQA1*01:01-DQB1*06:18',
         'HLA-DQA1*01:01-DQB1*06:19', 'HLA-DQA1*01:01-DQB1*06:21', 'HLA-DQA1*01:01-DQB1*06:22', 'HLA-DQA1*01:01-DQB1*06:23',
         'HLA-DQA1*01:01-DQB1*06:24', 'HLA-DQA1*01:01-DQB1*06:25',
         'HLA-DQA1*01:01-DQB1*06:27', 'HLA-DQA1*01:01-DQB1*06:28', 'HLA-DQA1*01:01-DQB1*06:29', 'HLA-DQA1*01:01-DQB1*06:30',
         'HLA-DQA1*01:01-DQB1*06:31', 'HLA-DQA1*01:01-DQB1*06:32',
         'HLA-DQA1*01:01-DQB1*06:33', 'HLA-DQA1*01:01-DQB1*06:34', 'HLA-DQA1*01:01-DQB1*06:35', 'HLA-DQA1*01:01-DQB1*06:36',
         'HLA-DQA1*01:01-DQB1*06:37', 'HLA-DQA1*01:01-DQB1*06:38',
         'HLA-DQA1*01:01-DQB1*06:39', 'HLA-DQA1*01:01-DQB1*06:40', 'HLA-DQA1*01:01-DQB1*06:41', 'HLA-DQA1*01:01-DQB1*06:42',
         'HLA-DQA1*01:01-DQB1*06:43', 'HLA-DQA1*01:01-DQB1*06:44',
         'HLA-DQA1*01:02-DQB1*02:01', 'HLA-DQA1*01:02-DQB1*02:02', 'HLA-DQA1*01:02-DQB1*02:03', 'HLA-DQA1*01:02-DQB1*02:04',
         'HLA-DQA1*01:02-DQB1*02:05', 'HLA-DQA1*01:02-DQB1*02:06',
         'HLA-DQA1*01:02-DQB1*03:01', 'HLA-DQA1*01:02-DQB1*03:02', 'HLA-DQA1*01:02-DQB1*03:03', 'HLA-DQA1*01:02-DQB1*03:04',
         'HLA-DQA1*01:02-DQB1*03:05', 'HLA-DQA1*01:02-DQB1*03:06',
         'HLA-DQA1*01:02-DQB1*03:07', 'HLA-DQA1*01:02-DQB1*03:08', 'HLA-DQA1*01:02-DQB1*03:09', 'HLA-DQA1*01:02-DQB1*03:10',
         'HLA-DQA1*01:02-DQB1*03:11', 'HLA-DQA1*01:02-DQB1*03:12',
         'HLA-DQA1*01:02-DQB1*03:13', 'HLA-DQA1*01:02-DQB1*03:14', 'HLA-DQA1*01:02-DQB1*03:15', 'HLA-DQA1*01:02-DQB1*03:16',
         'HLA-DQA1*01:02-DQB1*03:17', 'HLA-DQA1*01:02-DQB1*03:18',
         'HLA-DQA1*01:02-DQB1*03:19', 'HLA-DQA1*01:02-DQB1*03:20', 'HLA-DQA1*01:02-DQB1*03:21', 'HLA-DQA1*01:02-DQB1*03:22',
         'HLA-DQA1*01:02-DQB1*03:23', 'HLA-DQA1*01:02-DQB1*03:24',
         'HLA-DQA1*01:02-DQB1*03:25', 'HLA-DQA1*01:02-DQB1*03:26', 'HLA-DQA1*01:02-DQB1*03:27', 'HLA-DQA1*01:02-DQB1*03:28',
         'HLA-DQA1*01:02-DQB1*03:29', 'HLA-DQA1*01:02-DQB1*03:30',
         'HLA-DQA1*01:02-DQB1*03:31', 'HLA-DQA1*01:02-DQB1*03:32', 'HLA-DQA1*01:02-DQB1*03:33', 'HLA-DQA1*01:02-DQB1*03:34',
         'HLA-DQA1*01:02-DQB1*03:35', 'HLA-DQA1*01:02-DQB1*03:36',
         'HLA-DQA1*01:02-DQB1*03:37', 'HLA-DQA1*01:02-DQB1*03:38', 'HLA-DQA1*01:02-DQB1*04:01', 'HLA-DQA1*01:02-DQB1*04:02',
         'HLA-DQA1*01:02-DQB1*04:03', 'HLA-DQA1*01:02-DQB1*04:04',
         'HLA-DQA1*01:02-DQB1*04:05', 'HLA-DQA1*01:02-DQB1*04:06', 'HLA-DQA1*01:02-DQB1*04:07', 'HLA-DQA1*01:02-DQB1*04:08',
         'HLA-DQA1*01:02-DQB1*05:01', 'HLA-DQA1*01:02-DQB1*05:02',
         'HLA-DQA1*01:02-DQB1*05:03', 'HLA-DQA1*01:02-DQB1*05:05', 'HLA-DQA1*01:02-DQB1*05:06', 'HLA-DQA1*01:02-DQB1*05:07',
         'HLA-DQA1*01:02-DQB1*05:08', 'HLA-DQA1*01:02-DQB1*05:09',
         'HLA-DQA1*01:02-DQB1*05:10', 'HLA-DQA1*01:02-DQB1*05:11', 'HLA-DQA1*01:02-DQB1*05:12', 'HLA-DQA1*01:02-DQB1*05:13',
         'HLA-DQA1*01:02-DQB1*05:14', 'HLA-DQA1*01:02-DQB1*06:01',
         'HLA-DQA1*01:02-DQB1*06:02', 'HLA-DQA1*01:02-DQB1*06:03', 'HLA-DQA1*01:02-DQB1*06:04', 'HLA-DQA1*01:02-DQB1*06:07',
         'HLA-DQA1*01:02-DQB1*06:08', 'HLA-DQA1*01:02-DQB1*06:09',
         'HLA-DQA1*01:02-DQB1*06:10', 'HLA-DQA1*01:02-DQB1*06:11', 'HLA-DQA1*01:02-DQB1*06:12', 'HLA-DQA1*01:02-DQB1*06:14',
         'HLA-DQA1*01:02-DQB1*06:15', 'HLA-DQA1*01:02-DQB1*06:16',
         'HLA-DQA1*01:02-DQB1*06:17', 'HLA-DQA1*01:02-DQB1*06:18', 'HLA-DQA1*01:02-DQB1*06:19', 'HLA-DQA1*01:02-DQB1*06:21',
         'HLA-DQA1*01:02-DQB1*06:22', 'HLA-DQA1*01:02-DQB1*06:23',
         'HLA-DQA1*01:02-DQB1*06:24', 'HLA-DQA1*01:02-DQB1*06:25', 'HLA-DQA1*01:02-DQB1*06:27', 'HLA-DQA1*01:02-DQB1*06:28',
         'HLA-DQA1*01:02-DQB1*06:29', 'HLA-DQA1*01:02-DQB1*06:30',
         'HLA-DQA1*01:02-DQB1*06:31', 'HLA-DQA1*01:02-DQB1*06:32', 'HLA-DQA1*01:02-DQB1*06:33', 'HLA-DQA1*01:02-DQB1*06:34',
         'HLA-DQA1*01:02-DQB1*06:35', 'HLA-DQA1*01:02-DQB1*06:36',
         'HLA-DQA1*01:02-DQB1*06:37', 'HLA-DQA1*01:02-DQB1*06:38', 'HLA-DQA1*01:02-DQB1*06:39', 'HLA-DQA1*01:02-DQB1*06:40',
         'HLA-DQA1*01:02-DQB1*06:41', 'HLA-DQA1*01:02-DQB1*06:42',
         'HLA-DQA1*01:02-DQB1*06:43', 'HLA-DQA1*01:02-DQB1*06:44', 'HLA-DQA1*01:03-DQB1*02:01', 'HLA-DQA1*01:03-DQB1*02:02',
         'HLA-DQA1*01:03-DQB1*02:03', 'HLA-DQA1*01:03-DQB1*02:04',
         'HLA-DQA1*01:03-DQB1*02:05', 'HLA-DQA1*01:03-DQB1*02:06', 'HLA-DQA1*01:03-DQB1*03:01', 'HLA-DQA1*01:03-DQB1*03:02',
         'HLA-DQA1*01:03-DQB1*03:03', 'HLA-DQA1*01:03-DQB1*03:04',
         'HLA-DQA1*01:03-DQB1*03:05', 'HLA-DQA1*01:03-DQB1*03:06', 'HLA-DQA1*01:03-DQB1*03:07', 'HLA-DQA1*01:03-DQB1*03:08',
         'HLA-DQA1*01:03-DQB1*03:09', 'HLA-DQA1*01:03-DQB1*03:10',
         'HLA-DQA1*01:03-DQB1*03:11', 'HLA-DQA1*01:03-DQB1*03:12', 'HLA-DQA1*01:03-DQB1*03:13', 'HLA-DQA1*01:03-DQB1*03:14',
         'HLA-DQA1*01:03-DQB1*03:15', 'HLA-DQA1*01:03-DQB1*03:16',
         'HLA-DQA1*01:03-DQB1*03:17', 'HLA-DQA1*01:03-DQB1*03:18', 'HLA-DQA1*01:03-DQB1*03:19', 'HLA-DQA1*01:03-DQB1*03:20',
         'HLA-DQA1*01:03-DQB1*03:21', 'HLA-DQA1*01:03-DQB1*03:22',
         'HLA-DQA1*01:03-DQB1*03:23', 'HLA-DQA1*01:03-DQB1*03:24', 'HLA-DQA1*01:03-DQB1*03:25', 'HLA-DQA1*01:03-DQB1*03:26',
         'HLA-DQA1*01:03-DQB1*03:27', 'HLA-DQA1*01:03-DQB1*03:28',
         'HLA-DQA1*01:03-DQB1*03:29', 'HLA-DQA1*01:03-DQB1*03:30', 'HLA-DQA1*01:03-DQB1*03:31', 'HLA-DQA1*01:03-DQB1*03:32',
         'HLA-DQA1*01:03-DQB1*03:33', 'HLA-DQA1*01:03-DQB1*03:34',
         'HLA-DQA1*01:03-DQB1*03:35', 'HLA-DQA1*01:03-DQB1*03:36', 'HLA-DQA1*01:03-DQB1*03:37', 'HLA-DQA1*01:03-DQB1*03:38',
         'HLA-DQA1*01:03-DQB1*04:01', 'HLA-DQA1*01:03-DQB1*04:02',
         'HLA-DQA1*01:03-DQB1*04:03', 'HLA-DQA1*01:03-DQB1*04:04', 'HLA-DQA1*01:03-DQB1*04:05', 'HLA-DQA1*01:03-DQB1*04:06',
         'HLA-DQA1*01:03-DQB1*04:07', 'HLA-DQA1*01:03-DQB1*04:08',
         'HLA-DQA1*01:03-DQB1*05:01', 'HLA-DQA1*01:03-DQB1*05:02', 'HLA-DQA1*01:03-DQB1*05:03', 'HLA-DQA1*01:03-DQB1*05:05',
         'HLA-DQA1*01:03-DQB1*05:06', 'HLA-DQA1*01:03-DQB1*05:07',
         'HLA-DQA1*01:03-DQB1*05:08', 'HLA-DQA1*01:03-DQB1*05:09', 'HLA-DQA1*01:03-DQB1*05:10', 'HLA-DQA1*01:03-DQB1*05:11',
         'HLA-DQA1*01:03-DQB1*05:12', 'HLA-DQA1*01:03-DQB1*05:13',
         'HLA-DQA1*01:03-DQB1*05:14', 'HLA-DQA1*01:03-DQB1*06:01', 'HLA-DQA1*01:03-DQB1*06:02', 'HLA-DQA1*01:03-DQB1*06:03',
         'HLA-DQA1*01:03-DQB1*06:04', 'HLA-DQA1*01:03-DQB1*06:07',
         'HLA-DQA1*01:03-DQB1*06:08', 'HLA-DQA1*01:03-DQB1*06:09', 'HLA-DQA1*01:03-DQB1*06:10', 'HLA-DQA1*01:03-DQB1*06:11',
         'HLA-DQA1*01:03-DQB1*06:12', 'HLA-DQA1*01:03-DQB1*06:14',
         'HLA-DQA1*01:03-DQB1*06:15', 'HLA-DQA1*01:03-DQB1*06:16', 'HLA-DQA1*01:03-DQB1*06:17', 'HLA-DQA1*01:03-DQB1*06:18',
         'HLA-DQA1*01:03-DQB1*06:19', 'HLA-DQA1*01:03-DQB1*06:21',
         'HLA-DQA1*01:03-DQB1*06:22', 'HLA-DQA1*01:03-DQB1*06:23', 'HLA-DQA1*01:03-DQB1*06:24', 'HLA-DQA1*01:03-DQB1*06:25',
         'HLA-DQA1*01:03-DQB1*06:27', 'HLA-DQA1*01:03-DQB1*06:28',
         'HLA-DQA1*01:03-DQB1*06:29', 'HLA-DQA1*01:03-DQB1*06:30', 'HLA-DQA1*01:03-DQB1*06:31', 'HLA-DQA1*01:03-DQB1*06:32',
         'HLA-DQA1*01:03-DQB1*06:33', 'HLA-DQA1*01:03-DQB1*06:34',
         'HLA-DQA1*01:03-DQB1*06:35', 'HLA-DQA1*01:03-DQB1*06:36', 'HLA-DQA1*01:03-DQB1*06:37', 'HLA-DQA1*01:03-DQB1*06:38',
         'HLA-DQA1*01:03-DQB1*06:39', 'HLA-DQA1*01:03-DQB1*06:40',
         'HLA-DQA1*01:03-DQB1*06:41', 'HLA-DQA1*01:03-DQB1*06:42', 'HLA-DQA1*01:03-DQB1*06:43', 'HLA-DQA1*01:03-DQB1*06:44',
         'HLA-DQA1*01:04-DQB1*02:01', 'HLA-DQA1*01:04-DQB1*02:02',
         'HLA-DQA1*01:04-DQB1*02:03', 'HLA-DQA1*01:04-DQB1*02:04', 'HLA-DQA1*01:04-DQB1*02:05', 'HLA-DQA1*01:04-DQB1*02:06',
         'HLA-DQA1*01:04-DQB1*03:01', 'HLA-DQA1*01:04-DQB1*03:02',
         'HLA-DQA1*01:04-DQB1*03:03', 'HLA-DQA1*01:04-DQB1*03:04', 'HLA-DQA1*01:04-DQB1*03:05', 'HLA-DQA1*01:04-DQB1*03:06',
         'HLA-DQA1*01:04-DQB1*03:07', 'HLA-DQA1*01:04-DQB1*03:08',
         'HLA-DQA1*01:04-DQB1*03:09', 'HLA-DQA1*01:04-DQB1*03:10', 'HLA-DQA1*01:04-DQB1*03:11', 'HLA-DQA1*01:04-DQB1*03:12',
         'HLA-DQA1*01:04-DQB1*03:13', 'HLA-DQA1*01:04-DQB1*03:14',
         'HLA-DQA1*01:04-DQB1*03:15', 'HLA-DQA1*01:04-DQB1*03:16', 'HLA-DQA1*01:04-DQB1*03:17', 'HLA-DQA1*01:04-DQB1*03:18',
         'HLA-DQA1*01:04-DQB1*03:19', 'HLA-DQA1*01:04-DQB1*03:20',
         'HLA-DQA1*01:04-DQB1*03:21', 'HLA-DQA1*01:04-DQB1*03:22', 'HLA-DQA1*01:04-DQB1*03:23', 'HLA-DQA1*01:04-DQB1*03:24',
         'HLA-DQA1*01:04-DQB1*03:25', 'HLA-DQA1*01:04-DQB1*03:26',
         'HLA-DQA1*01:04-DQB1*03:27', 'HLA-DQA1*01:04-DQB1*03:28', 'HLA-DQA1*01:04-DQB1*03:29', 'HLA-DQA1*01:04-DQB1*03:30',
         'HLA-DQA1*01:04-DQB1*03:31', 'HLA-DQA1*01:04-DQB1*03:32',
         'HLA-DQA1*01:04-DQB1*03:33', 'HLA-DQA1*01:04-DQB1*03:34', 'HLA-DQA1*01:04-DQB1*03:35', 'HLA-DQA1*01:04-DQB1*03:36',
         'HLA-DQA1*01:04-DQB1*03:37', 'HLA-DQA1*01:04-DQB1*03:38',
         'HLA-DQA1*01:04-DQB1*04:01', 'HLA-DQA1*01:04-DQB1*04:02', 'HLA-DQA1*01:04-DQB1*04:03', 'HLA-DQA1*01:04-DQB1*04:04',
         'HLA-DQA1*01:04-DQB1*04:05', 'HLA-DQA1*01:04-DQB1*04:06',
         'HLA-DQA1*01:04-DQB1*04:07', 'HLA-DQA1*01:04-DQB1*04:08', 'HLA-DQA1*01:04-DQB1*05:01', 'HLA-DQA1*01:04-DQB1*05:02',
         'HLA-DQA1*01:04-DQB1*05:03', 'HLA-DQA1*01:04-DQB1*05:05',
         'HLA-DQA1*01:04-DQB1*05:06', 'HLA-DQA1*01:04-DQB1*05:07', 'HLA-DQA1*01:04-DQB1*05:08', 'HLA-DQA1*01:04-DQB1*05:09',
         'HLA-DQA1*01:04-DQB1*05:10', 'HLA-DQA1*01:04-DQB1*05:11',
         'HLA-DQA1*01:04-DQB1*05:12', 'HLA-DQA1*01:04-DQB1*05:13', 'HLA-DQA1*01:04-DQB1*05:14', 'HLA-DQA1*01:04-DQB1*06:01',
         'HLA-DQA1*01:04-DQB1*06:02', 'HLA-DQA1*01:04-DQB1*06:03',
         'HLA-DQA1*01:04-DQB1*06:04', 'HLA-DQA1*01:04-DQB1*06:07', 'HLA-DQA1*01:04-DQB1*06:08', 'HLA-DQA1*01:04-DQB1*06:09',
         'HLA-DQA1*01:04-DQB1*06:10', 'HLA-DQA1*01:04-DQB1*06:11',
         'HLA-DQA1*01:04-DQB1*06:12', 'HLA-DQA1*01:04-DQB1*06:14', 'HLA-DQA1*01:04-DQB1*06:15', 'HLA-DQA1*01:04-DQB1*06:16',
         'HLA-DQA1*01:04-DQB1*06:17', 'HLA-DQA1*01:04-DQB1*06:18',
         'HLA-DQA1*01:04-DQB1*06:19', 'HLA-DQA1*01:04-DQB1*06:21', 'HLA-DQA1*01:04-DQB1*06:22', 'HLA-DQA1*01:04-DQB1*06:23',
         'HLA-DQA1*01:04-DQB1*06:24', 'HLA-DQA1*01:04-DQB1*06:25',
         'HLA-DQA1*01:04-DQB1*06:27', 'HLA-DQA1*01:04-DQB1*06:28', 'HLA-DQA1*01:04-DQB1*06:29', 'HLA-DQA1*01:04-DQB1*06:30',
         'HLA-DQA1*01:04-DQB1*06:31', 'HLA-DQA1*01:04-DQB1*06:32',
         'HLA-DQA1*01:04-DQB1*06:33', 'HLA-DQA1*01:04-DQB1*06:34', 'HLA-DQA1*01:04-DQB1*06:35', 'HLA-DQA1*01:04-DQB1*06:36',
         'HLA-DQA1*01:04-DQB1*06:37', 'HLA-DQA1*01:04-DQB1*06:38',
         'HLA-DQA1*01:04-DQB1*06:39', 'HLA-DQA1*01:04-DQB1*06:40', 'HLA-DQA1*01:04-DQB1*06:41', 'HLA-DQA1*01:04-DQB1*06:42',
         'HLA-DQA1*01:04-DQB1*06:43', 'HLA-DQA1*01:04-DQB1*06:44',
         'HLA-DQA1*01:05-DQB1*02:01', 'HLA-DQA1*01:05-DQB1*02:02', 'HLA-DQA1*01:05-DQB1*02:03', 'HLA-DQA1*01:05-DQB1*02:04',
         'HLA-DQA1*01:05-DQB1*02:05', 'HLA-DQA1*01:05-DQB1*02:06',
         'HLA-DQA1*01:05-DQB1*03:01', 'HLA-DQA1*01:05-DQB1*03:02', 'HLA-DQA1*01:05-DQB1*03:03', 'HLA-DQA1*01:05-DQB1*03:04',
         'HLA-DQA1*01:05-DQB1*03:05', 'HLA-DQA1*01:05-DQB1*03:06',
         'HLA-DQA1*01:05-DQB1*03:07', 'HLA-DQA1*01:05-DQB1*03:08', 'HLA-DQA1*01:05-DQB1*03:09', 'HLA-DQA1*01:05-DQB1*03:10',
         'HLA-DQA1*01:05-DQB1*03:11', 'HLA-DQA1*01:05-DQB1*03:12',
         'HLA-DQA1*01:05-DQB1*03:13', 'HLA-DQA1*01:05-DQB1*03:14', 'HLA-DQA1*01:05-DQB1*03:15', 'HLA-DQA1*01:05-DQB1*03:16',
         'HLA-DQA1*01:05-DQB1*03:17', 'HLA-DQA1*01:05-DQB1*03:18',
         'HLA-DQA1*01:05-DQB1*03:19', 'HLA-DQA1*01:05-DQB1*03:20', 'HLA-DQA1*01:05-DQB1*03:21', 'HLA-DQA1*01:05-DQB1*03:22',
         'HLA-DQA1*01:05-DQB1*03:23', 'HLA-DQA1*01:05-DQB1*03:24',
         'HLA-DQA1*01:05-DQB1*03:25', 'HLA-DQA1*01:05-DQB1*03:26', 'HLA-DQA1*01:05-DQB1*03:27', 'HLA-DQA1*01:05-DQB1*03:28',
         'HLA-DQA1*01:05-DQB1*03:29', 'HLA-DQA1*01:05-DQB1*03:30',
         'HLA-DQA1*01:05-DQB1*03:31', 'HLA-DQA1*01:05-DQB1*03:32', 'HLA-DQA1*01:05-DQB1*03:33', 'HLA-DQA1*01:05-DQB1*03:34',
         'HLA-DQA1*01:05-DQB1*03:35', 'HLA-DQA1*01:05-DQB1*03:36',
         'HLA-DQA1*01:05-DQB1*03:37', 'HLA-DQA1*01:05-DQB1*03:38', 'HLA-DQA1*01:05-DQB1*04:01', 'HLA-DQA1*01:05-DQB1*04:02',
         'HLA-DQA1*01:05-DQB1*04:03', 'HLA-DQA1*01:05-DQB1*04:04',
         'HLA-DQA1*01:05-DQB1*04:05', 'HLA-DQA1*01:05-DQB1*04:06', 'HLA-DQA1*01:05-DQB1*04:07', 'HLA-DQA1*01:05-DQB1*04:08',
         'HLA-DQA1*01:05-DQB1*05:01', 'HLA-DQA1*01:05-DQB1*05:02',
         'HLA-DQA1*01:05-DQB1*05:03', 'HLA-DQA1*01:05-DQB1*05:05', 'HLA-DQA1*01:05-DQB1*05:06', 'HLA-DQA1*01:05-DQB1*05:07',
         'HLA-DQA1*01:05-DQB1*05:08', 'HLA-DQA1*01:05-DQB1*05:09',
         'HLA-DQA1*01:05-DQB1*05:10', 'HLA-DQA1*01:05-DQB1*05:11', 'HLA-DQA1*01:05-DQB1*05:12', 'HLA-DQA1*01:05-DQB1*05:13',
         'HLA-DQA1*01:05-DQB1*05:14', 'HLA-DQA1*01:05-DQB1*06:01',
         'HLA-DQA1*01:05-DQB1*06:02', 'HLA-DQA1*01:05-DQB1*06:03', 'HLA-DQA1*01:05-DQB1*06:04', 'HLA-DQA1*01:05-DQB1*06:07',
         'HLA-DQA1*01:05-DQB1*06:08', 'HLA-DQA1*01:05-DQB1*06:09',
         'HLA-DQA1*01:05-DQB1*06:10', 'HLA-DQA1*01:05-DQB1*06:11', 'HLA-DQA1*01:05-DQB1*06:12', 'HLA-DQA1*01:05-DQB1*06:14',
         'HLA-DQA1*01:05-DQB1*06:15', 'HLA-DQA1*01:05-DQB1*06:16',
         'HLA-DQA1*01:05-DQB1*06:17', 'HLA-DQA1*01:05-DQB1*06:18', 'HLA-DQA1*01:05-DQB1*06:19', 'HLA-DQA1*01:05-DQB1*06:21',
         'HLA-DQA1*01:05-DQB1*06:22', 'HLA-DQA1*01:05-DQB1*06:23',
         'HLA-DQA1*01:05-DQB1*06:24', 'HLA-DQA1*01:05-DQB1*06:25', 'HLA-DQA1*01:05-DQB1*06:27', 'HLA-DQA1*01:05-DQB1*06:28',
         'HLA-DQA1*01:05-DQB1*06:29', 'HLA-DQA1*01:05-DQB1*06:30',
         'HLA-DQA1*01:05-DQB1*06:31', 'HLA-DQA1*01:05-DQB1*06:32', 'HLA-DQA1*01:05-DQB1*06:33', 'HLA-DQA1*01:05-DQB1*06:34',
         'HLA-DQA1*01:05-DQB1*06:35', 'HLA-DQA1*01:05-DQB1*06:36',
         'HLA-DQA1*01:05-DQB1*06:37', 'HLA-DQA1*01:05-DQB1*06:38', 'HLA-DQA1*01:05-DQB1*06:39', 'HLA-DQA1*01:05-DQB1*06:40',
         'HLA-DQA1*01:05-DQB1*06:41', 'HLA-DQA1*01:05-DQB1*06:42',
         'HLA-DQA1*01:05-DQB1*06:43', 'HLA-DQA1*01:05-DQB1*06:44', 'HLA-DQA1*01:06-DQB1*02:01', 'HLA-DQA1*01:06-DQB1*02:02',
         'HLA-DQA1*01:06-DQB1*02:03', 'HLA-DQA1*01:06-DQB1*02:04',
         'HLA-DQA1*01:06-DQB1*02:05', 'HLA-DQA1*01:06-DQB1*02:06', 'HLA-DQA1*01:06-DQB1*03:01', 'HLA-DQA1*01:06-DQB1*03:02',
         'HLA-DQA1*01:06-DQB1*03:03', 'HLA-DQA1*01:06-DQB1*03:04',
         'HLA-DQA1*01:06-DQB1*03:05', 'HLA-DQA1*01:06-DQB1*03:06', 'HLA-DQA1*01:06-DQB1*03:07', 'HLA-DQA1*01:06-DQB1*03:08',
         'HLA-DQA1*01:06-DQB1*03:09', 'HLA-DQA1*01:06-DQB1*03:10',
         'HLA-DQA1*01:06-DQB1*03:11', 'HLA-DQA1*01:06-DQB1*03:12', 'HLA-DQA1*01:06-DQB1*03:13', 'HLA-DQA1*01:06-DQB1*03:14',
         'HLA-DQA1*01:06-DQB1*03:15', 'HLA-DQA1*01:06-DQB1*03:16',
         'HLA-DQA1*01:06-DQB1*03:17', 'HLA-DQA1*01:06-DQB1*03:18', 'HLA-DQA1*01:06-DQB1*03:19', 'HLA-DQA1*01:06-DQB1*03:20',
         'HLA-DQA1*01:06-DQB1*03:21', 'HLA-DQA1*01:06-DQB1*03:22',
         'HLA-DQA1*01:06-DQB1*03:23', 'HLA-DQA1*01:06-DQB1*03:24', 'HLA-DQA1*01:06-DQB1*03:25', 'HLA-DQA1*01:06-DQB1*03:26',
         'HLA-DQA1*01:06-DQB1*03:27', 'HLA-DQA1*01:06-DQB1*03:28',
         'HLA-DQA1*01:06-DQB1*03:29', 'HLA-DQA1*01:06-DQB1*03:30', 'HLA-DQA1*01:06-DQB1*03:31', 'HLA-DQA1*01:06-DQB1*03:32',
         'HLA-DQA1*01:06-DQB1*03:33', 'HLA-DQA1*01:06-DQB1*03:34',
         'HLA-DQA1*01:06-DQB1*03:35', 'HLA-DQA1*01:06-DQB1*03:36', 'HLA-DQA1*01:06-DQB1*03:37', 'HLA-DQA1*01:06-DQB1*03:38',
         'HLA-DQA1*01:06-DQB1*04:01', 'HLA-DQA1*01:06-DQB1*04:02',
         'HLA-DQA1*01:06-DQB1*04:03', 'HLA-DQA1*01:06-DQB1*04:04', 'HLA-DQA1*01:06-DQB1*04:05', 'HLA-DQA1*01:06-DQB1*04:06',
         'HLA-DQA1*01:06-DQB1*04:07', 'HLA-DQA1*01:06-DQB1*04:08',
         'HLA-DQA1*01:06-DQB1*05:01', 'HLA-DQA1*01:06-DQB1*05:02', 'HLA-DQA1*01:06-DQB1*05:03', 'HLA-DQA1*01:06-DQB1*05:05',
         'HLA-DQA1*01:06-DQB1*05:06', 'HLA-DQA1*01:06-DQB1*05:07',
         'HLA-DQA1*01:06-DQB1*05:08', 'HLA-DQA1*01:06-DQB1*05:09', 'HLA-DQA1*01:06-DQB1*05:10', 'HLA-DQA1*01:06-DQB1*05:11',
         'HLA-DQA1*01:06-DQB1*05:12', 'HLA-DQA1*01:06-DQB1*05:13',
         'HLA-DQA1*01:06-DQB1*05:14', 'HLA-DQA1*01:06-DQB1*06:01', 'HLA-DQA1*01:06-DQB1*06:02', 'HLA-DQA1*01:06-DQB1*06:03',
         'HLA-DQA1*01:06-DQB1*06:04', 'HLA-DQA1*01:06-DQB1*06:07',
         'HLA-DQA1*01:06-DQB1*06:08', 'HLA-DQA1*01:06-DQB1*06:09', 'HLA-DQA1*01:06-DQB1*06:10', 'HLA-DQA1*01:06-DQB1*06:11',
         'HLA-DQA1*01:06-DQB1*06:12', 'HLA-DQA1*01:06-DQB1*06:14',
         'HLA-DQA1*01:06-DQB1*06:15', 'HLA-DQA1*01:06-DQB1*06:16', 'HLA-DQA1*01:06-DQB1*06:17', 'HLA-DQA1*01:06-DQB1*06:18',
         'HLA-DQA1*01:06-DQB1*06:19', 'HLA-DQA1*01:06-DQB1*06:21',
         'HLA-DQA1*01:06-DQB1*06:22', 'HLA-DQA1*01:06-DQB1*06:23', 'HLA-DQA1*01:06-DQB1*06:24', 'HLA-DQA1*01:06-DQB1*06:25',
         'HLA-DQA1*01:06-DQB1*06:27', 'HLA-DQA1*01:06-DQB1*06:28',
         'HLA-DQA1*01:06-DQB1*06:29', 'HLA-DQA1*01:06-DQB1*06:30', 'HLA-DQA1*01:06-DQB1*06:31', 'HLA-DQA1*01:06-DQB1*06:32',
         'HLA-DQA1*01:06-DQB1*06:33', 'HLA-DQA1*01:06-DQB1*06:34',
         'HLA-DQA1*01:06-DQB1*06:35', 'HLA-DQA1*01:06-DQB1*06:36', 'HLA-DQA1*01:06-DQB1*06:37', 'HLA-DQA1*01:06-DQB1*06:38',
         'HLA-DQA1*01:06-DQB1*06:39', 'HLA-DQA1*01:06-DQB1*06:40',
         'HLA-DQA1*01:06-DQB1*06:41', 'HLA-DQA1*01:06-DQB1*06:42', 'HLA-DQA1*01:06-DQB1*06:43', 'HLA-DQA1*01:06-DQB1*06:44',
         'HLA-DQA1*01:07-DQB1*02:01', 'HLA-DQA1*01:07-DQB1*02:02',
         'HLA-DQA1*01:07-DQB1*02:03', 'HLA-DQA1*01:07-DQB1*02:04', 'HLA-DQA1*01:07-DQB1*02:05', 'HLA-DQA1*01:07-DQB1*02:06',
         'HLA-DQA1*01:07-DQB1*03:01', 'HLA-DQA1*01:07-DQB1*03:02',
         'HLA-DQA1*01:07-DQB1*03:03', 'HLA-DQA1*01:07-DQB1*03:04', 'HLA-DQA1*01:07-DQB1*03:05', 'HLA-DQA1*01:07-DQB1*03:06',
         'HLA-DQA1*01:07-DQB1*03:07', 'HLA-DQA1*01:07-DQB1*03:08',
         'HLA-DQA1*01:07-DQB1*03:09', 'HLA-DQA1*01:07-DQB1*03:10', 'HLA-DQA1*01:07-DQB1*03:11', 'HLA-DQA1*01:07-DQB1*03:12',
         'HLA-DQA1*01:07-DQB1*03:13', 'HLA-DQA1*01:07-DQB1*03:14',
         'HLA-DQA1*01:07-DQB1*03:15', 'HLA-DQA1*01:07-DQB1*03:16', 'HLA-DQA1*01:07-DQB1*03:17', 'HLA-DQA1*01:07-DQB1*03:18',
         'HLA-DQA1*01:07-DQB1*03:19', 'HLA-DQA1*01:07-DQB1*03:20',
         'HLA-DQA1*01:07-DQB1*03:21', 'HLA-DQA1*01:07-DQB1*03:22', 'HLA-DQA1*01:07-DQB1*03:23', 'HLA-DQA1*01:07-DQB1*03:24',
         'HLA-DQA1*01:07-DQB1*03:25', 'HLA-DQA1*01:07-DQB1*03:26',
         'HLA-DQA1*01:07-DQB1*03:27', 'HLA-DQA1*01:07-DQB1*03:28', 'HLA-DQA1*01:07-DQB1*03:29', 'HLA-DQA1*01:07-DQB1*03:30',
         'HLA-DQA1*01:07-DQB1*03:31', 'HLA-DQA1*01:07-DQB1*03:32',
         'HLA-DQA1*01:07-DQB1*03:33', 'HLA-DQA1*01:07-DQB1*03:34', 'HLA-DQA1*01:07-DQB1*03:35', 'HLA-DQA1*01:07-DQB1*03:36',
         'HLA-DQA1*01:07-DQB1*03:37', 'HLA-DQA1*01:07-DQB1*03:38',
         'HLA-DQA1*01:07-DQB1*04:01', 'HLA-DQA1*01:07-DQB1*04:02', 'HLA-DQA1*01:07-DQB1*04:03', 'HLA-DQA1*01:07-DQB1*04:04',
         'HLA-DQA1*01:07-DQB1*04:05', 'HLA-DQA1*01:07-DQB1*04:06',
         'HLA-DQA1*01:07-DQB1*04:07', 'HLA-DQA1*01:07-DQB1*04:08', 'HLA-DQA1*01:07-DQB1*05:01', 'HLA-DQA1*01:07-DQB1*05:02',
         'HLA-DQA1*01:07-DQB1*05:03', 'HLA-DQA1*01:07-DQB1*05:05',
         'HLA-DQA1*01:07-DQB1*05:06', 'HLA-DQA1*01:07-DQB1*05:07', 'HLA-DQA1*01:07-DQB1*05:08', 'HLA-DQA1*01:07-DQB1*05:09',
         'HLA-DQA1*01:07-DQB1*05:10', 'HLA-DQA1*01:07-DQB1*05:11',
         'HLA-DQA1*01:07-DQB1*05:12', 'HLA-DQA1*01:07-DQB1*05:13', 'HLA-DQA1*01:07-DQB1*05:14', 'HLA-DQA1*01:07-DQB1*06:01',
         'HLA-DQA1*01:07-DQB1*06:02', 'HLA-DQA1*01:07-DQB1*06:03',
         'HLA-DQA1*01:07-DQB1*06:04', 'HLA-DQA1*01:07-DQB1*06:07', 'HLA-DQA1*01:07-DQB1*06:08', 'HLA-DQA1*01:07-DQB1*06:09',
         'HLA-DQA1*01:07-DQB1*06:10', 'HLA-DQA1*01:07-DQB1*06:11',
         'HLA-DQA1*01:07-DQB1*06:12', 'HLA-DQA1*01:07-DQB1*06:14', 'HLA-DQA1*01:07-DQB1*06:15', 'HLA-DQA1*01:07-DQB1*06:16',
         'HLA-DQA1*01:07-DQB1*06:17', 'HLA-DQA1*01:07-DQB1*06:18',
         'HLA-DQA1*01:07-DQB1*06:19', 'HLA-DQA1*01:07-DQB1*06:21', 'HLA-DQA1*01:07-DQB1*06:22', 'HLA-DQA1*01:07-DQB1*06:23',
         'HLA-DQA1*01:07-DQB1*06:24', 'HLA-DQA1*01:07-DQB1*06:25',
         'HLA-DQA1*01:07-DQB1*06:27', 'HLA-DQA1*01:07-DQB1*06:28', 'HLA-DQA1*01:07-DQB1*06:29', 'HLA-DQA1*01:07-DQB1*06:30',
         'HLA-DQA1*01:07-DQB1*06:31', 'HLA-DQA1*01:07-DQB1*06:32',
         'HLA-DQA1*01:07-DQB1*06:33', 'HLA-DQA1*01:07-DQB1*06:34', 'HLA-DQA1*01:07-DQB1*06:35', 'HLA-DQA1*01:07-DQB1*06:36',
         'HLA-DQA1*01:07-DQB1*06:37', 'HLA-DQA1*01:07-DQB1*06:38',
         'HLA-DQA1*01:07-DQB1*06:39', 'HLA-DQA1*01:07-DQB1*06:40', 'HLA-DQA1*01:07-DQB1*06:41', 'HLA-DQA1*01:07-DQB1*06:42',
         'HLA-DQA1*01:07-DQB1*06:43', 'HLA-DQA1*01:07-DQB1*06:44',
         'HLA-DQA1*01:08-DQB1*02:01', 'HLA-DQA1*01:08-DQB1*02:02', 'HLA-DQA1*01:08-DQB1*02:03', 'HLA-DQA1*01:08-DQB1*02:04',
         'HLA-DQA1*01:08-DQB1*02:05', 'HLA-DQA1*01:08-DQB1*02:06',
         'HLA-DQA1*01:08-DQB1*03:01', 'HLA-DQA1*01:08-DQB1*03:02', 'HLA-DQA1*01:08-DQB1*03:03', 'HLA-DQA1*01:08-DQB1*03:04',
         'HLA-DQA1*01:08-DQB1*03:05', 'HLA-DQA1*01:08-DQB1*03:06',
         'HLA-DQA1*01:08-DQB1*03:07', 'HLA-DQA1*01:08-DQB1*03:08', 'HLA-DQA1*01:08-DQB1*03:09', 'HLA-DQA1*01:08-DQB1*03:10',
         'HLA-DQA1*01:08-DQB1*03:11', 'HLA-DQA1*01:08-DQB1*03:12',
         'HLA-DQA1*01:08-DQB1*03:13', 'HLA-DQA1*01:08-DQB1*03:14', 'HLA-DQA1*01:08-DQB1*03:15', 'HLA-DQA1*01:08-DQB1*03:16',
         'HLA-DQA1*01:08-DQB1*03:17', 'HLA-DQA1*01:08-DQB1*03:18',
         'HLA-DQA1*01:08-DQB1*03:19', 'HLA-DQA1*01:08-DQB1*03:20', 'HLA-DQA1*01:08-DQB1*03:21', 'HLA-DQA1*01:08-DQB1*03:22',
         'HLA-DQA1*01:08-DQB1*03:23', 'HLA-DQA1*01:08-DQB1*03:24',
         'HLA-DQA1*01:08-DQB1*03:25', 'HLA-DQA1*01:08-DQB1*03:26', 'HLA-DQA1*01:08-DQB1*03:27', 'HLA-DQA1*01:08-DQB1*03:28',
         'HLA-DQA1*01:08-DQB1*03:29', 'HLA-DQA1*01:08-DQB1*03:30',
         'HLA-DQA1*01:08-DQB1*03:31', 'HLA-DQA1*01:08-DQB1*03:32', 'HLA-DQA1*01:08-DQB1*03:33', 'HLA-DQA1*01:08-DQB1*03:34',
         'HLA-DQA1*01:08-DQB1*03:35', 'HLA-DQA1*01:08-DQB1*03:36',
         'HLA-DQA1*01:08-DQB1*03:37', 'HLA-DQA1*01:08-DQB1*03:38', 'HLA-DQA1*01:08-DQB1*04:01', 'HLA-DQA1*01:08-DQB1*04:02',
         'HLA-DQA1*01:08-DQB1*04:03', 'HLA-DQA1*01:08-DQB1*04:04',
         'HLA-DQA1*01:08-DQB1*04:05', 'HLA-DQA1*01:08-DQB1*04:06', 'HLA-DQA1*01:08-DQB1*04:07', 'HLA-DQA1*01:08-DQB1*04:08',
         'HLA-DQA1*01:08-DQB1*05:01', 'HLA-DQA1*01:08-DQB1*05:02',
         'HLA-DQA1*01:08-DQB1*05:03', 'HLA-DQA1*01:08-DQB1*05:05', 'HLA-DQA1*01:08-DQB1*05:06', 'HLA-DQA1*01:08-DQB1*05:07',
         'HLA-DQA1*01:08-DQB1*05:08', 'HLA-DQA1*01:08-DQB1*05:09',
         'HLA-DQA1*01:08-DQB1*05:10', 'HLA-DQA1*01:08-DQB1*05:11', 'HLA-DQA1*01:08-DQB1*05:12', 'HLA-DQA1*01:08-DQB1*05:13',
         'HLA-DQA1*01:08-DQB1*05:14', 'HLA-DQA1*01:08-DQB1*06:01',
         'HLA-DQA1*01:08-DQB1*06:02', 'HLA-DQA1*01:08-DQB1*06:03', 'HLA-DQA1*01:08-DQB1*06:04', 'HLA-DQA1*01:08-DQB1*06:07',
         'HLA-DQA1*01:08-DQB1*06:08', 'HLA-DQA1*01:08-DQB1*06:09',
         'HLA-DQA1*01:08-DQB1*06:10', 'HLA-DQA1*01:08-DQB1*06:11', 'HLA-DQA1*01:08-DQB1*06:12', 'HLA-DQA1*01:08-DQB1*06:14',
         'HLA-DQA1*01:08-DQB1*06:15', 'HLA-DQA1*01:08-DQB1*06:16',
         'HLA-DQA1*01:08-DQB1*06:17', 'HLA-DQA1*01:08-DQB1*06:18', 'HLA-DQA1*01:08-DQB1*06:19', 'HLA-DQA1*01:08-DQB1*06:21',
         'HLA-DQA1*01:08-DQB1*06:22', 'HLA-DQA1*01:08-DQB1*06:23',
         'HLA-DQA1*01:08-DQB1*06:24', 'HLA-DQA1*01:08-DQB1*06:25', 'HLA-DQA1*01:08-DQB1*06:27', 'HLA-DQA1*01:08-DQB1*06:28',
         'HLA-DQA1*01:08-DQB1*06:29', 'HLA-DQA1*01:08-DQB1*06:30',
         'HLA-DQA1*01:08-DQB1*06:31', 'HLA-DQA1*01:08-DQB1*06:32', 'HLA-DQA1*01:08-DQB1*06:33', 'HLA-DQA1*01:08-DQB1*06:34',
         'HLA-DQA1*01:08-DQB1*06:35', 'HLA-DQA1*01:08-DQB1*06:36',
         'HLA-DQA1*01:08-DQB1*06:37', 'HLA-DQA1*01:08-DQB1*06:38', 'HLA-DQA1*01:08-DQB1*06:39', 'HLA-DQA1*01:08-DQB1*06:40',
         'HLA-DQA1*01:08-DQB1*06:41', 'HLA-DQA1*01:08-DQB1*06:42',
         'HLA-DQA1*01:08-DQB1*06:43', 'HLA-DQA1*01:08-DQB1*06:44', 'HLA-DQA1*01:09-DQB1*02:01', 'HLA-DQA1*01:09-DQB1*02:02',
         'HLA-DQA1*01:09-DQB1*02:03', 'HLA-DQA1*01:09-DQB1*02:04',
         'HLA-DQA1*01:09-DQB1*02:05', 'HLA-DQA1*01:09-DQB1*02:06', 'HLA-DQA1*01:09-DQB1*03:01', 'HLA-DQA1*01:09-DQB1*03:02',
         'HLA-DQA1*01:09-DQB1*03:03', 'HLA-DQA1*01:09-DQB1*03:04',
         'HLA-DQA1*01:09-DQB1*03:05', 'HLA-DQA1*01:09-DQB1*03:06', 'HLA-DQA1*01:09-DQB1*03:07', 'HLA-DQA1*01:09-DQB1*03:08',
         'HLA-DQA1*01:09-DQB1*03:09', 'HLA-DQA1*01:09-DQB1*03:10',
         'HLA-DQA1*01:09-DQB1*03:11', 'HLA-DQA1*01:09-DQB1*03:12', 'HLA-DQA1*01:09-DQB1*03:13', 'HLA-DQA1*01:09-DQB1*03:14',
         'HLA-DQA1*01:09-DQB1*03:15', 'HLA-DQA1*01:09-DQB1*03:16',
         'HLA-DQA1*01:09-DQB1*03:17', 'HLA-DQA1*01:09-DQB1*03:18', 'HLA-DQA1*01:09-DQB1*03:19', 'HLA-DQA1*01:09-DQB1*03:20',
         'HLA-DQA1*01:09-DQB1*03:21', 'HLA-DQA1*01:09-DQB1*03:22',
         'HLA-DQA1*01:09-DQB1*03:23', 'HLA-DQA1*01:09-DQB1*03:24', 'HLA-DQA1*01:09-DQB1*03:25', 'HLA-DQA1*01:09-DQB1*03:26',
         'HLA-DQA1*01:09-DQB1*03:27', 'HLA-DQA1*01:09-DQB1*03:28',
         'HLA-DQA1*01:09-DQB1*03:29', 'HLA-DQA1*01:09-DQB1*03:30', 'HLA-DQA1*01:09-DQB1*03:31', 'HLA-DQA1*01:09-DQB1*03:32',
         'HLA-DQA1*01:09-DQB1*03:33', 'HLA-DQA1*01:09-DQB1*03:34',
         'HLA-DQA1*01:09-DQB1*03:35', 'HLA-DQA1*01:09-DQB1*03:36', 'HLA-DQA1*01:09-DQB1*03:37', 'HLA-DQA1*01:09-DQB1*03:38',
         'HLA-DQA1*01:09-DQB1*04:01', 'HLA-DQA1*01:09-DQB1*04:02',
         'HLA-DQA1*01:09-DQB1*04:03', 'HLA-DQA1*01:09-DQB1*04:04', 'HLA-DQA1*01:09-DQB1*04:05', 'HLA-DQA1*01:09-DQB1*04:06',
         'HLA-DQA1*01:09-DQB1*04:07', 'HLA-DQA1*01:09-DQB1*04:08',
         'HLA-DQA1*01:09-DQB1*05:01', 'HLA-DQA1*01:09-DQB1*05:02', 'HLA-DQA1*01:09-DQB1*05:03', 'HLA-DQA1*01:09-DQB1*05:05',
         'HLA-DQA1*01:09-DQB1*05:06', 'HLA-DQA1*01:09-DQB1*05:07',
         'HLA-DQA1*01:09-DQB1*05:08', 'HLA-DQA1*01:09-DQB1*05:09', 'HLA-DQA1*01:09-DQB1*05:10', 'HLA-DQA1*01:09-DQB1*05:11',
         'HLA-DQA1*01:09-DQB1*05:12', 'HLA-DQA1*01:09-DQB1*05:13',
         'HLA-DQA1*01:09-DQB1*05:14', 'HLA-DQA1*01:09-DQB1*06:01', 'HLA-DQA1*01:09-DQB1*06:02', 'HLA-DQA1*01:09-DQB1*06:03',
         'HLA-DQA1*01:09-DQB1*06:04', 'HLA-DQA1*01:09-DQB1*06:07',
         'HLA-DQA1*01:09-DQB1*06:08', 'HLA-DQA1*01:09-DQB1*06:09', 'HLA-DQA1*01:09-DQB1*06:10', 'HLA-DQA1*01:09-DQB1*06:11',
         'HLA-DQA1*01:09-DQB1*06:12', 'HLA-DQA1*01:09-DQB1*06:14',
         'HLA-DQA1*01:09-DQB1*06:15', 'HLA-DQA1*01:09-DQB1*06:16', 'HLA-DQA1*01:09-DQB1*06:17', 'HLA-DQA1*01:09-DQB1*06:18',
         'HLA-DQA1*01:09-DQB1*06:19', 'HLA-DQA1*01:09-DQB1*06:21',
         'HLA-DQA1*01:09-DQB1*06:22', 'HLA-DQA1*01:09-DQB1*06:23', 'HLA-DQA1*01:09-DQB1*06:24', 'HLA-DQA1*01:09-DQB1*06:25',
         'HLA-DQA1*01:09-DQB1*06:27', 'HLA-DQA1*01:09-DQB1*06:28',
         'HLA-DQA1*01:09-DQB1*06:29', 'HLA-DQA1*01:09-DQB1*06:30', 'HLA-DQA1*01:09-DQB1*06:31', 'HLA-DQA1*01:09-DQB1*06:32',
         'HLA-DQA1*01:09-DQB1*06:33', 'HLA-DQA1*01:09-DQB1*06:34',
         'HLA-DQA1*01:09-DQB1*06:35', 'HLA-DQA1*01:09-DQB1*06:36', 'HLA-DQA1*01:09-DQB1*06:37', 'HLA-DQA1*01:09-DQB1*06:38',
         'HLA-DQA1*01:09-DQB1*06:39', 'HLA-DQA1*01:09-DQB1*06:40',
         'HLA-DQA1*01:09-DQB1*06:41', 'HLA-DQA1*01:09-DQB1*06:42', 'HLA-DQA1*01:09-DQB1*06:43', 'HLA-DQA1*01:09-DQB1*06:44',
         'HLA-DQA1*02:01-DQB1*02:01', 'HLA-DQA1*02:01-DQB1*02:02',
         'HLA-DQA1*02:01-DQB1*02:03', 'HLA-DQA1*02:01-DQB1*02:04', 'HLA-DQA1*02:01-DQB1*02:05', 'HLA-DQA1*02:01-DQB1*02:06',
         'HLA-DQA1*02:01-DQB1*03:01', 'HLA-DQA1*02:01-DQB1*03:02',
         'HLA-DQA1*02:01-DQB1*03:03', 'HLA-DQA1*02:01-DQB1*03:04', 'HLA-DQA1*02:01-DQB1*03:05', 'HLA-DQA1*02:01-DQB1*03:06',
         'HLA-DQA1*02:01-DQB1*03:07', 'HLA-DQA1*02:01-DQB1*03:08',
         'HLA-DQA1*02:01-DQB1*03:09', 'HLA-DQA1*02:01-DQB1*03:10', 'HLA-DQA1*02:01-DQB1*03:11', 'HLA-DQA1*02:01-DQB1*03:12',
         'HLA-DQA1*02:01-DQB1*03:13', 'HLA-DQA1*02:01-DQB1*03:14',
         'HLA-DQA1*02:01-DQB1*03:15', 'HLA-DQA1*02:01-DQB1*03:16', 'HLA-DQA1*02:01-DQB1*03:17', 'HLA-DQA1*02:01-DQB1*03:18',
         'HLA-DQA1*02:01-DQB1*03:19', 'HLA-DQA1*02:01-DQB1*03:20',
         'HLA-DQA1*02:01-DQB1*03:21', 'HLA-DQA1*02:01-DQB1*03:22', 'HLA-DQA1*02:01-DQB1*03:23', 'HLA-DQA1*02:01-DQB1*03:24',
         'HLA-DQA1*02:01-DQB1*03:25', 'HLA-DQA1*02:01-DQB1*03:26',
         'HLA-DQA1*02:01-DQB1*03:27', 'HLA-DQA1*02:01-DQB1*03:28', 'HLA-DQA1*02:01-DQB1*03:29', 'HLA-DQA1*02:01-DQB1*03:30',
         'HLA-DQA1*02:01-DQB1*03:31', 'HLA-DQA1*02:01-DQB1*03:32',
         'HLA-DQA1*02:01-DQB1*03:33', 'HLA-DQA1*02:01-DQB1*03:34', 'HLA-DQA1*02:01-DQB1*03:35', 'HLA-DQA1*02:01-DQB1*03:36',
         'HLA-DQA1*02:01-DQB1*03:37', 'HLA-DQA1*02:01-DQB1*03:38',
         'HLA-DQA1*02:01-DQB1*04:01', 'HLA-DQA1*02:01-DQB1*04:02', 'HLA-DQA1*02:01-DQB1*04:03', 'HLA-DQA1*02:01-DQB1*04:04',
         'HLA-DQA1*02:01-DQB1*04:05', 'HLA-DQA1*02:01-DQB1*04:06',
         'HLA-DQA1*02:01-DQB1*04:07', 'HLA-DQA1*02:01-DQB1*04:08', 'HLA-DQA1*02:01-DQB1*05:01', 'HLA-DQA1*02:01-DQB1*05:02',
         'HLA-DQA1*02:01-DQB1*05:03', 'HLA-DQA1*02:01-DQB1*05:05',
         'HLA-DQA1*02:01-DQB1*05:06', 'HLA-DQA1*02:01-DQB1*05:07', 'HLA-DQA1*02:01-DQB1*05:08', 'HLA-DQA1*02:01-DQB1*05:09',
         'HLA-DQA1*02:01-DQB1*05:10', 'HLA-DQA1*02:01-DQB1*05:11',
         'HLA-DQA1*02:01-DQB1*05:12', 'HLA-DQA1*02:01-DQB1*05:13', 'HLA-DQA1*02:01-DQB1*05:14', 'HLA-DQA1*02:01-DQB1*06:01',
         'HLA-DQA1*02:01-DQB1*06:02', 'HLA-DQA1*02:01-DQB1*06:03',
         'HLA-DQA1*02:01-DQB1*06:04', 'HLA-DQA1*02:01-DQB1*06:07', 'HLA-DQA1*02:01-DQB1*06:08', 'HLA-DQA1*02:01-DQB1*06:09',
         'HLA-DQA1*02:01-DQB1*06:10', 'HLA-DQA1*02:01-DQB1*06:11',
         'HLA-DQA1*02:01-DQB1*06:12', 'HLA-DQA1*02:01-DQB1*06:14', 'HLA-DQA1*02:01-DQB1*06:15', 'HLA-DQA1*02:01-DQB1*06:16',
         'HLA-DQA1*02:01-DQB1*06:17', 'HLA-DQA1*02:01-DQB1*06:18',
         'HLA-DQA1*02:01-DQB1*06:19', 'HLA-DQA1*02:01-DQB1*06:21', 'HLA-DQA1*02:01-DQB1*06:22', 'HLA-DQA1*02:01-DQB1*06:23',
         'HLA-DQA1*02:01-DQB1*06:24', 'HLA-DQA1*02:01-DQB1*06:25',
         'HLA-DQA1*02:01-DQB1*06:27', 'HLA-DQA1*02:01-DQB1*06:28', 'HLA-DQA1*02:01-DQB1*06:29', 'HLA-DQA1*02:01-DQB1*06:30',
         'HLA-DQA1*02:01-DQB1*06:31', 'HLA-DQA1*02:01-DQB1*06:32',
         'HLA-DQA1*02:01-DQB1*06:33', 'HLA-DQA1*02:01-DQB1*06:34', 'HLA-DQA1*02:01-DQB1*06:35', 'HLA-DQA1*02:01-DQB1*06:36',
         'HLA-DQA1*02:01-DQB1*06:37', 'HLA-DQA1*02:01-DQB1*06:38',
         'HLA-DQA1*02:01-DQB1*06:39', 'HLA-DQA1*02:01-DQB1*06:40', 'HLA-DQA1*02:01-DQB1*06:41', 'HLA-DQA1*02:01-DQB1*06:42',
         'HLA-DQA1*02:01-DQB1*06:43', 'HLA-DQA1*02:01-DQB1*06:44',
         'HLA-DQA1*03:01-DQB1*02:01', 'HLA-DQA1*03:01-DQB1*02:02', 'HLA-DQA1*03:01-DQB1*02:03', 'HLA-DQA1*03:01-DQB1*02:04',
         'HLA-DQA1*03:01-DQB1*02:05', 'HLA-DQA1*03:01-DQB1*02:06',
         'HLA-DQA1*03:01-DQB1*03:01', 'HLA-DQA1*03:01-DQB1*03:02', 'HLA-DQA1*03:01-DQB1*03:03', 'HLA-DQA1*03:01-DQB1*03:04',
         'HLA-DQA1*03:01-DQB1*03:05', 'HLA-DQA1*03:01-DQB1*03:06',
         'HLA-DQA1*03:01-DQB1*03:07', 'HLA-DQA1*03:01-DQB1*03:08', 'HLA-DQA1*03:01-DQB1*03:09', 'HLA-DQA1*03:01-DQB1*03:10',
         'HLA-DQA1*03:01-DQB1*03:11', 'HLA-DQA1*03:01-DQB1*03:12',
         'HLA-DQA1*03:01-DQB1*03:13', 'HLA-DQA1*03:01-DQB1*03:14', 'HLA-DQA1*03:01-DQB1*03:15', 'HLA-DQA1*03:01-DQB1*03:16',
         'HLA-DQA1*03:01-DQB1*03:17', 'HLA-DQA1*03:01-DQB1*03:18',
         'HLA-DQA1*03:01-DQB1*03:19', 'HLA-DQA1*03:01-DQB1*03:20', 'HLA-DQA1*03:01-DQB1*03:21', 'HLA-DQA1*03:01-DQB1*03:22',
         'HLA-DQA1*03:01-DQB1*03:23', 'HLA-DQA1*03:01-DQB1*03:24',
         'HLA-DQA1*03:01-DQB1*03:25', 'HLA-DQA1*03:01-DQB1*03:26', 'HLA-DQA1*03:01-DQB1*03:27', 'HLA-DQA1*03:01-DQB1*03:28',
         'HLA-DQA1*03:01-DQB1*03:29', 'HLA-DQA1*03:01-DQB1*03:30',
         'HLA-DQA1*03:01-DQB1*03:31', 'HLA-DQA1*03:01-DQB1*03:32', 'HLA-DQA1*03:01-DQB1*03:33', 'HLA-DQA1*03:01-DQB1*03:34',
         'HLA-DQA1*03:01-DQB1*03:35', 'HLA-DQA1*03:01-DQB1*03:36',
         'HLA-DQA1*03:01-DQB1*03:37', 'HLA-DQA1*03:01-DQB1*03:38', 'HLA-DQA1*03:01-DQB1*04:01', 'HLA-DQA1*03:01-DQB1*04:02',
         'HLA-DQA1*03:01-DQB1*04:03', 'HLA-DQA1*03:01-DQB1*04:04',
         'HLA-DQA1*03:01-DQB1*04:05', 'HLA-DQA1*03:01-DQB1*04:06', 'HLA-DQA1*03:01-DQB1*04:07', 'HLA-DQA1*03:01-DQB1*04:08',
         'HLA-DQA1*03:01-DQB1*05:01', 'HLA-DQA1*03:01-DQB1*05:02',
         'HLA-DQA1*03:01-DQB1*05:03', 'HLA-DQA1*03:01-DQB1*05:05', 'HLA-DQA1*03:01-DQB1*05:06', 'HLA-DQA1*03:01-DQB1*05:07',
         'HLA-DQA1*03:01-DQB1*05:08', 'HLA-DQA1*03:01-DQB1*05:09',
         'HLA-DQA1*03:01-DQB1*05:10', 'HLA-DQA1*03:01-DQB1*05:11', 'HLA-DQA1*03:01-DQB1*05:12', 'HLA-DQA1*03:01-DQB1*05:13',
         'HLA-DQA1*03:01-DQB1*05:14', 'HLA-DQA1*03:01-DQB1*06:01',
         'HLA-DQA1*03:01-DQB1*06:02', 'HLA-DQA1*03:01-DQB1*06:03', 'HLA-DQA1*03:01-DQB1*06:04', 'HLA-DQA1*03:01-DQB1*06:07',
         'HLA-DQA1*03:01-DQB1*06:08', 'HLA-DQA1*03:01-DQB1*06:09',
         'HLA-DQA1*03:01-DQB1*06:10', 'HLA-DQA1*03:01-DQB1*06:11', 'HLA-DQA1*03:01-DQB1*06:12', 'HLA-DQA1*03:01-DQB1*06:14',
         'HLA-DQA1*03:01-DQB1*06:15', 'HLA-DQA1*03:01-DQB1*06:16',
         'HLA-DQA1*03:01-DQB1*06:17', 'HLA-DQA1*03:01-DQB1*06:18', 'HLA-DQA1*03:01-DQB1*06:19', 'HLA-DQA1*03:01-DQB1*06:21',
         'HLA-DQA1*03:01-DQB1*06:22', 'HLA-DQA1*03:01-DQB1*06:23',
         'HLA-DQA1*03:01-DQB1*06:24', 'HLA-DQA1*03:01-DQB1*06:25', 'HLA-DQA1*03:01-DQB1*06:27', 'HLA-DQA1*03:01-DQB1*06:28',
         'HLA-DQA1*03:01-DQB1*06:29', 'HLA-DQA1*03:01-DQB1*06:30',
         'HLA-DQA1*03:01-DQB1*06:31', 'HLA-DQA1*03:01-DQB1*06:32', 'HLA-DQA1*03:01-DQB1*06:33', 'HLA-DQA1*03:01-DQB1*06:34',
         'HLA-DQA1*03:01-DQB1*06:35', 'HLA-DQA1*03:01-DQB1*06:36',
         'HLA-DQA1*03:01-DQB1*06:37', 'HLA-DQA1*03:01-DQB1*06:38', 'HLA-DQA1*03:01-DQB1*06:39', 'HLA-DQA1*03:01-DQB1*06:40',
         'HLA-DQA1*03:01-DQB1*06:41', 'HLA-DQA1*03:01-DQB1*06:42',
         'HLA-DQA1*03:01-DQB1*06:43', 'HLA-DQA1*03:01-DQB1*06:44', 'HLA-DQA1*03:02-DQB1*02:01', 'HLA-DQA1*03:02-DQB1*02:02',
         'HLA-DQA1*03:02-DQB1*02:03', 'HLA-DQA1*03:02-DQB1*02:04',
         'HLA-DQA1*03:02-DQB1*02:05', 'HLA-DQA1*03:02-DQB1*02:06', 'HLA-DQA1*03:02-DQB1*03:01', 'HLA-DQA1*03:02-DQB1*03:02',
         'HLA-DQA1*03:02-DQB1*03:03', 'HLA-DQA1*03:02-DQB1*03:04',
         'HLA-DQA1*03:02-DQB1*03:05', 'HLA-DQA1*03:02-DQB1*03:06', 'HLA-DQA1*03:02-DQB1*03:07', 'HLA-DQA1*03:02-DQB1*03:08',
         'HLA-DQA1*03:02-DQB1*03:09', 'HLA-DQA1*03:02-DQB1*03:10',
         'HLA-DQA1*03:02-DQB1*03:11', 'HLA-DQA1*03:02-DQB1*03:12', 'HLA-DQA1*03:02-DQB1*03:13', 'HLA-DQA1*03:02-DQB1*03:14',
         'HLA-DQA1*03:02-DQB1*03:15', 'HLA-DQA1*03:02-DQB1*03:16',
         'HLA-DQA1*03:02-DQB1*03:17', 'HLA-DQA1*03:02-DQB1*03:18', 'HLA-DQA1*03:02-DQB1*03:19', 'HLA-DQA1*03:02-DQB1*03:20',
         'HLA-DQA1*03:02-DQB1*03:21', 'HLA-DQA1*03:02-DQB1*03:22',
         'HLA-DQA1*03:02-DQB1*03:23', 'HLA-DQA1*03:02-DQB1*03:24', 'HLA-DQA1*03:02-DQB1*03:25', 'HLA-DQA1*03:02-DQB1*03:26',
         'HLA-DQA1*03:02-DQB1*03:27', 'HLA-DQA1*03:02-DQB1*03:28',
         'HLA-DQA1*03:02-DQB1*03:29', 'HLA-DQA1*03:02-DQB1*03:30', 'HLA-DQA1*03:02-DQB1*03:31', 'HLA-DQA1*03:02-DQB1*03:32',
         'HLA-DQA1*03:02-DQB1*03:33', 'HLA-DQA1*03:02-DQB1*03:34',
         'HLA-DQA1*03:02-DQB1*03:35', 'HLA-DQA1*03:02-DQB1*03:36', 'HLA-DQA1*03:02-DQB1*03:37', 'HLA-DQA1*03:02-DQB1*03:38',
         'HLA-DQA1*03:02-DQB1*04:01', 'HLA-DQA1*03:02-DQB1*04:02',
         'HLA-DQA1*03:02-DQB1*04:03', 'HLA-DQA1*03:02-DQB1*04:04', 'HLA-DQA1*03:02-DQB1*04:05', 'HLA-DQA1*03:02-DQB1*04:06',
         'HLA-DQA1*03:02-DQB1*04:07', 'HLA-DQA1*03:02-DQB1*04:08',
         'HLA-DQA1*03:02-DQB1*05:01', 'HLA-DQA1*03:02-DQB1*05:02', 'HLA-DQA1*03:02-DQB1*05:03', 'HLA-DQA1*03:02-DQB1*05:05',
         'HLA-DQA1*03:02-DQB1*05:06', 'HLA-DQA1*03:02-DQB1*05:07',
         'HLA-DQA1*03:02-DQB1*05:08', 'HLA-DQA1*03:02-DQB1*05:09', 'HLA-DQA1*03:02-DQB1*05:10', 'HLA-DQA1*03:02-DQB1*05:11',
         'HLA-DQA1*03:02-DQB1*05:12', 'HLA-DQA1*03:02-DQB1*05:13',
         'HLA-DQA1*03:02-DQB1*05:14', 'HLA-DQA1*03:02-DQB1*06:01', 'HLA-DQA1*03:02-DQB1*06:02', 'HLA-DQA1*03:02-DQB1*06:03',
         'HLA-DQA1*03:02-DQB1*06:04', 'HLA-DQA1*03:02-DQB1*06:07',
         'HLA-DQA1*03:02-DQB1*06:08', 'HLA-DQA1*03:02-DQB1*06:09', 'HLA-DQA1*03:02-DQB1*06:10', 'HLA-DQA1*03:02-DQB1*06:11',
         'HLA-DQA1*03:02-DQB1*06:12', 'HLA-DQA1*03:02-DQB1*06:14',
         'HLA-DQA1*03:02-DQB1*06:15', 'HLA-DQA1*03:02-DQB1*06:16', 'HLA-DQA1*03:02-DQB1*06:17', 'HLA-DQA1*03:02-DQB1*06:18',
         'HLA-DQA1*03:02-DQB1*06:19', 'HLA-DQA1*03:02-DQB1*06:21',
         'HLA-DQA1*03:02-DQB1*06:22', 'HLA-DQA1*03:02-DQB1*06:23', 'HLA-DQA1*03:02-DQB1*06:24', 'HLA-DQA1*03:02-DQB1*06:25',
         'HLA-DQA1*03:02-DQB1*06:27', 'HLA-DQA1*03:02-DQB1*06:28',
         'HLA-DQA1*03:02-DQB1*06:29', 'HLA-DQA1*03:02-DQB1*06:30', 'HLA-DQA1*03:02-DQB1*06:31', 'HLA-DQA1*03:02-DQB1*06:32',
         'HLA-DQA1*03:02-DQB1*06:33', 'HLA-DQA1*03:02-DQB1*06:34',
         'HLA-DQA1*03:02-DQB1*06:35', 'HLA-DQA1*03:02-DQB1*06:36', 'HLA-DQA1*03:02-DQB1*06:37', 'HLA-DQA1*03:02-DQB1*06:38',
         'HLA-DQA1*03:02-DQB1*06:39', 'HLA-DQA1*03:02-DQB1*06:40',
         'HLA-DQA1*03:02-DQB1*06:41', 'HLA-DQA1*03:02-DQB1*06:42', 'HLA-DQA1*03:02-DQB1*06:43', 'HLA-DQA1*03:02-DQB1*06:44',
         'HLA-DQA1*03:03-DQB1*02:01', 'HLA-DQA1*03:03-DQB1*02:02',
         'HLA-DQA1*03:03-DQB1*02:03', 'HLA-DQA1*03:03-DQB1*02:04', 'HLA-DQA1*03:03-DQB1*02:05', 'HLA-DQA1*03:03-DQB1*02:06',
         'HLA-DQA1*03:03-DQB1*03:01', 'HLA-DQA1*03:03-DQB1*03:02',
         'HLA-DQA1*03:03-DQB1*03:03', 'HLA-DQA1*03:03-DQB1*03:04', 'HLA-DQA1*03:03-DQB1*03:05', 'HLA-DQA1*03:03-DQB1*03:06',
         'HLA-DQA1*03:03-DQB1*03:07', 'HLA-DQA1*03:03-DQB1*03:08',
         'HLA-DQA1*03:03-DQB1*03:09', 'HLA-DQA1*03:03-DQB1*03:10', 'HLA-DQA1*03:03-DQB1*03:11', 'HLA-DQA1*03:03-DQB1*03:12',
         'HLA-DQA1*03:03-DQB1*03:13', 'HLA-DQA1*03:03-DQB1*03:14',
         'HLA-DQA1*03:03-DQB1*03:15', 'HLA-DQA1*03:03-DQB1*03:16', 'HLA-DQA1*03:03-DQB1*03:17', 'HLA-DQA1*03:03-DQB1*03:18',
         'HLA-DQA1*03:03-DQB1*03:19', 'HLA-DQA1*03:03-DQB1*03:20',
         'HLA-DQA1*03:03-DQB1*03:21', 'HLA-DQA1*03:03-DQB1*03:22', 'HLA-DQA1*03:03-DQB1*03:23', 'HLA-DQA1*03:03-DQB1*03:24',
         'HLA-DQA1*03:03-DQB1*03:25', 'HLA-DQA1*03:03-DQB1*03:26',
         'HLA-DQA1*03:03-DQB1*03:27', 'HLA-DQA1*03:03-DQB1*03:28', 'HLA-DQA1*03:03-DQB1*03:29', 'HLA-DQA1*03:03-DQB1*03:30',
         'HLA-DQA1*03:03-DQB1*03:31', 'HLA-DQA1*03:03-DQB1*03:32',
         'HLA-DQA1*03:03-DQB1*03:33', 'HLA-DQA1*03:03-DQB1*03:34', 'HLA-DQA1*03:03-DQB1*03:35', 'HLA-DQA1*03:03-DQB1*03:36',
         'HLA-DQA1*03:03-DQB1*03:37', 'HLA-DQA1*03:03-DQB1*03:38',
         'HLA-DQA1*03:03-DQB1*04:01', 'HLA-DQA1*03:03-DQB1*04:02', 'HLA-DQA1*03:03-DQB1*04:03', 'HLA-DQA1*03:03-DQB1*04:04',
         'HLA-DQA1*03:03-DQB1*04:05', 'HLA-DQA1*03:03-DQB1*04:06',
         'HLA-DQA1*03:03-DQB1*04:07', 'HLA-DQA1*03:03-DQB1*04:08', 'HLA-DQA1*03:03-DQB1*05:01', 'HLA-DQA1*03:03-DQB1*05:02',
         'HLA-DQA1*03:03-DQB1*05:03', 'HLA-DQA1*03:03-DQB1*05:05',
         'HLA-DQA1*03:03-DQB1*05:06', 'HLA-DQA1*03:03-DQB1*05:07', 'HLA-DQA1*03:03-DQB1*05:08', 'HLA-DQA1*03:03-DQB1*05:09',
         'HLA-DQA1*03:03-DQB1*05:10', 'HLA-DQA1*03:03-DQB1*05:11',
         'HLA-DQA1*03:03-DQB1*05:12', 'HLA-DQA1*03:03-DQB1*05:13', 'HLA-DQA1*03:03-DQB1*05:14', 'HLA-DQA1*03:03-DQB1*06:01',
         'HLA-DQA1*03:03-DQB1*06:02', 'HLA-DQA1*03:03-DQB1*06:03',
         'HLA-DQA1*03:03-DQB1*06:04', 'HLA-DQA1*03:03-DQB1*06:07', 'HLA-DQA1*03:03-DQB1*06:08', 'HLA-DQA1*03:03-DQB1*06:09',
         'HLA-DQA1*03:03-DQB1*06:10', 'HLA-DQA1*03:03-DQB1*06:11',
         'HLA-DQA1*03:03-DQB1*06:12', 'HLA-DQA1*03:03-DQB1*06:14', 'HLA-DQA1*03:03-DQB1*06:15', 'HLA-DQA1*03:03-DQB1*06:16',
         'HLA-DQA1*03:03-DQB1*06:17', 'HLA-DQA1*03:03-DQB1*06:18',
         'HLA-DQA1*03:03-DQB1*06:19', 'HLA-DQA1*03:03-DQB1*06:21', 'HLA-DQA1*03:03-DQB1*06:22', 'HLA-DQA1*03:03-DQB1*06:23',
         'HLA-DQA1*03:03-DQB1*06:24', 'HLA-DQA1*03:03-DQB1*06:25',
         'HLA-DQA1*03:03-DQB1*06:27', 'HLA-DQA1*03:03-DQB1*06:28', 'HLA-DQA1*03:03-DQB1*06:29', 'HLA-DQA1*03:03-DQB1*06:30',
         'HLA-DQA1*03:03-DQB1*06:31', 'HLA-DQA1*03:03-DQB1*06:32',
         'HLA-DQA1*03:03-DQB1*06:33', 'HLA-DQA1*03:03-DQB1*06:34', 'HLA-DQA1*03:03-DQB1*06:35', 'HLA-DQA1*03:03-DQB1*06:36',
         'HLA-DQA1*03:03-DQB1*06:37', 'HLA-DQA1*03:03-DQB1*06:38',
         'HLA-DQA1*03:03-DQB1*06:39', 'HLA-DQA1*03:03-DQB1*06:40', 'HLA-DQA1*03:03-DQB1*06:41', 'HLA-DQA1*03:03-DQB1*06:42',
         'HLA-DQA1*03:03-DQB1*06:43', 'HLA-DQA1*03:03-DQB1*06:44',
         'HLA-DQA1*04:01-DQB1*02:01', 'HLA-DQA1*04:01-DQB1*02:02', 'HLA-DQA1*04:01-DQB1*02:03', 'HLA-DQA1*04:01-DQB1*02:04',
         'HLA-DQA1*04:01-DQB1*02:05', 'HLA-DQA1*04:01-DQB1*02:06',
         'HLA-DQA1*04:01-DQB1*03:01', 'HLA-DQA1*04:01-DQB1*03:02', 'HLA-DQA1*04:01-DQB1*03:03', 'HLA-DQA1*04:01-DQB1*03:04',
         'HLA-DQA1*04:01-DQB1*03:05', 'HLA-DQA1*04:01-DQB1*03:06',
         'HLA-DQA1*04:01-DQB1*03:07', 'HLA-DQA1*04:01-DQB1*03:08', 'HLA-DQA1*04:01-DQB1*03:09', 'HLA-DQA1*04:01-DQB1*03:10',
         'HLA-DQA1*04:01-DQB1*03:11', 'HLA-DQA1*04:01-DQB1*03:12',
         'HLA-DQA1*04:01-DQB1*03:13', 'HLA-DQA1*04:01-DQB1*03:14', 'HLA-DQA1*04:01-DQB1*03:15', 'HLA-DQA1*04:01-DQB1*03:16',
         'HLA-DQA1*04:01-DQB1*03:17', 'HLA-DQA1*04:01-DQB1*03:18',
         'HLA-DQA1*04:01-DQB1*03:19', 'HLA-DQA1*04:01-DQB1*03:20', 'HLA-DQA1*04:01-DQB1*03:21', 'HLA-DQA1*04:01-DQB1*03:22',
         'HLA-DQA1*04:01-DQB1*03:23', 'HLA-DQA1*04:01-DQB1*03:24',
         'HLA-DQA1*04:01-DQB1*03:25', 'HLA-DQA1*04:01-DQB1*03:26', 'HLA-DQA1*04:01-DQB1*03:27', 'HLA-DQA1*04:01-DQB1*03:28',
         'HLA-DQA1*04:01-DQB1*03:29', 'HLA-DQA1*04:01-DQB1*03:30',
         'HLA-DQA1*04:01-DQB1*03:31', 'HLA-DQA1*04:01-DQB1*03:32', 'HLA-DQA1*04:01-DQB1*03:33', 'HLA-DQA1*04:01-DQB1*03:34',
         'HLA-DQA1*04:01-DQB1*03:35', 'HLA-DQA1*04:01-DQB1*03:36',
         'HLA-DQA1*04:01-DQB1*03:37', 'HLA-DQA1*04:01-DQB1*03:38', 'HLA-DQA1*04:01-DQB1*04:01', 'HLA-DQA1*04:01-DQB1*04:02',
         'HLA-DQA1*04:01-DQB1*04:03', 'HLA-DQA1*04:01-DQB1*04:04',
         'HLA-DQA1*04:01-DQB1*04:05', 'HLA-DQA1*04:01-DQB1*04:06', 'HLA-DQA1*04:01-DQB1*04:07', 'HLA-DQA1*04:01-DQB1*04:08',
         'HLA-DQA1*04:01-DQB1*05:01', 'HLA-DQA1*04:01-DQB1*05:02',
         'HLA-DQA1*04:01-DQB1*05:03', 'HLA-DQA1*04:01-DQB1*05:05', 'HLA-DQA1*04:01-DQB1*05:06', 'HLA-DQA1*04:01-DQB1*05:07',
         'HLA-DQA1*04:01-DQB1*05:08', 'HLA-DQA1*04:01-DQB1*05:09',
         'HLA-DQA1*04:01-DQB1*05:10', 'HLA-DQA1*04:01-DQB1*05:11', 'HLA-DQA1*04:01-DQB1*05:12', 'HLA-DQA1*04:01-DQB1*05:13',
         'HLA-DQA1*04:01-DQB1*05:14', 'HLA-DQA1*04:01-DQB1*06:01',
         'HLA-DQA1*04:01-DQB1*06:02', 'HLA-DQA1*04:01-DQB1*06:03', 'HLA-DQA1*04:01-DQB1*06:04', 'HLA-DQA1*04:01-DQB1*06:07',
         'HLA-DQA1*04:01-DQB1*06:08', 'HLA-DQA1*04:01-DQB1*06:09',
         'HLA-DQA1*04:01-DQB1*06:10', 'HLA-DQA1*04:01-DQB1*06:11', 'HLA-DQA1*04:01-DQB1*06:12', 'HLA-DQA1*04:01-DQB1*06:14',
         'HLA-DQA1*04:01-DQB1*06:15', 'HLA-DQA1*04:01-DQB1*06:16',
         'HLA-DQA1*04:01-DQB1*06:17', 'HLA-DQA1*04:01-DQB1*06:18', 'HLA-DQA1*04:01-DQB1*06:19', 'HLA-DQA1*04:01-DQB1*06:21',
         'HLA-DQA1*04:01-DQB1*06:22', 'HLA-DQA1*04:01-DQB1*06:23',
         'HLA-DQA1*04:01-DQB1*06:24', 'HLA-DQA1*04:01-DQB1*06:25', 'HLA-DQA1*04:01-DQB1*06:27', 'HLA-DQA1*04:01-DQB1*06:28',
         'HLA-DQA1*04:01-DQB1*06:29', 'HLA-DQA1*04:01-DQB1*06:30',
         'HLA-DQA1*04:01-DQB1*06:31', 'HLA-DQA1*04:01-DQB1*06:32', 'HLA-DQA1*04:01-DQB1*06:33', 'HLA-DQA1*04:01-DQB1*06:34',
         'HLA-DQA1*04:01-DQB1*06:35', 'HLA-DQA1*04:01-DQB1*06:36',
         'HLA-DQA1*04:01-DQB1*06:37', 'HLA-DQA1*04:01-DQB1*06:38', 'HLA-DQA1*04:01-DQB1*06:39', 'HLA-DQA1*04:01-DQB1*06:40',
         'HLA-DQA1*04:01-DQB1*06:41', 'HLA-DQA1*04:01-DQB1*06:42',
         'HLA-DQA1*04:01-DQB1*06:43', 'HLA-DQA1*04:01-DQB1*06:44', 'HLA-DQA1*04:02-DQB1*02:01', 'HLA-DQA1*04:02-DQB1*02:02',
         'HLA-DQA1*04:02-DQB1*02:03', 'HLA-DQA1*04:02-DQB1*02:04',
         'HLA-DQA1*04:02-DQB1*02:05', 'HLA-DQA1*04:02-DQB1*02:06', 'HLA-DQA1*04:02-DQB1*03:01', 'HLA-DQA1*04:02-DQB1*03:02',
         'HLA-DQA1*04:02-DQB1*03:03', 'HLA-DQA1*04:02-DQB1*03:04',
         'HLA-DQA1*04:02-DQB1*03:05', 'HLA-DQA1*04:02-DQB1*03:06', 'HLA-DQA1*04:02-DQB1*03:07', 'HLA-DQA1*04:02-DQB1*03:08',
         'HLA-DQA1*04:02-DQB1*03:09', 'HLA-DQA1*04:02-DQB1*03:10',
         'HLA-DQA1*04:02-DQB1*03:11', 'HLA-DQA1*04:02-DQB1*03:12', 'HLA-DQA1*04:02-DQB1*03:13', 'HLA-DQA1*04:02-DQB1*03:14',
         'HLA-DQA1*04:02-DQB1*03:15', 'HLA-DQA1*04:02-DQB1*03:16',
         'HLA-DQA1*04:02-DQB1*03:17', 'HLA-DQA1*04:02-DQB1*03:18', 'HLA-DQA1*04:02-DQB1*03:19', 'HLA-DQA1*04:02-DQB1*03:20',
         'HLA-DQA1*04:02-DQB1*03:21', 'HLA-DQA1*04:02-DQB1*03:22',
         'HLA-DQA1*04:02-DQB1*03:23', 'HLA-DQA1*04:02-DQB1*03:24', 'HLA-DQA1*04:02-DQB1*03:25', 'HLA-DQA1*04:02-DQB1*03:26',
         'HLA-DQA1*04:02-DQB1*03:27', 'HLA-DQA1*04:02-DQB1*03:28',
         'HLA-DQA1*04:02-DQB1*03:29', 'HLA-DQA1*04:02-DQB1*03:30', 'HLA-DQA1*04:02-DQB1*03:31', 'HLA-DQA1*04:02-DQB1*03:32',
         'HLA-DQA1*04:02-DQB1*03:33', 'HLA-DQA1*04:02-DQB1*03:34',
         'HLA-DQA1*04:02-DQB1*03:35', 'HLA-DQA1*04:02-DQB1*03:36', 'HLA-DQA1*04:02-DQB1*03:37', 'HLA-DQA1*04:02-DQB1*03:38',
         'HLA-DQA1*04:02-DQB1*04:01', 'HLA-DQA1*04:02-DQB1*04:02',
         'HLA-DQA1*04:02-DQB1*04:03', 'HLA-DQA1*04:02-DQB1*04:04', 'HLA-DQA1*04:02-DQB1*04:05', 'HLA-DQA1*04:02-DQB1*04:06',
         'HLA-DQA1*04:02-DQB1*04:07', 'HLA-DQA1*04:02-DQB1*04:08',
         'HLA-DQA1*04:02-DQB1*05:01', 'HLA-DQA1*04:02-DQB1*05:02', 'HLA-DQA1*04:02-DQB1*05:03', 'HLA-DQA1*04:02-DQB1*05:05',
         'HLA-DQA1*04:02-DQB1*05:06', 'HLA-DQA1*04:02-DQB1*05:07',
         'HLA-DQA1*04:02-DQB1*05:08', 'HLA-DQA1*04:02-DQB1*05:09', 'HLA-DQA1*04:02-DQB1*05:10', 'HLA-DQA1*04:02-DQB1*05:11',
         'HLA-DQA1*04:02-DQB1*05:12', 'HLA-DQA1*04:02-DQB1*05:13',
         'HLA-DQA1*04:02-DQB1*05:14', 'HLA-DQA1*04:02-DQB1*06:01', 'HLA-DQA1*04:02-DQB1*06:02', 'HLA-DQA1*04:02-DQB1*06:03',
         'HLA-DQA1*04:02-DQB1*06:04', 'HLA-DQA1*04:02-DQB1*06:07',
         'HLA-DQA1*04:02-DQB1*06:08', 'HLA-DQA1*04:02-DQB1*06:09', 'HLA-DQA1*04:02-DQB1*06:10', 'HLA-DQA1*04:02-DQB1*06:11',
         'HLA-DQA1*04:02-DQB1*06:12', 'HLA-DQA1*04:02-DQB1*06:14',
         'HLA-DQA1*04:02-DQB1*06:15', 'HLA-DQA1*04:02-DQB1*06:16', 'HLA-DQA1*04:02-DQB1*06:17', 'HLA-DQA1*04:02-DQB1*06:18',
         'HLA-DQA1*04:02-DQB1*06:19', 'HLA-DQA1*04:02-DQB1*06:21',
         'HLA-DQA1*04:02-DQB1*06:22', 'HLA-DQA1*04:02-DQB1*06:23', 'HLA-DQA1*04:02-DQB1*06:24', 'HLA-DQA1*04:02-DQB1*06:25',
         'HLA-DQA1*04:02-DQB1*06:27', 'HLA-DQA1*04:02-DQB1*06:28',
         'HLA-DQA1*04:02-DQB1*06:29', 'HLA-DQA1*04:02-DQB1*06:30', 'HLA-DQA1*04:02-DQB1*06:31', 'HLA-DQA1*04:02-DQB1*06:32',
         'HLA-DQA1*04:02-DQB1*06:33', 'HLA-DQA1*04:02-DQB1*06:34',
         'HLA-DQA1*04:02-DQB1*06:35', 'HLA-DQA1*04:02-DQB1*06:36', 'HLA-DQA1*04:02-DQB1*06:37', 'HLA-DQA1*04:02-DQB1*06:38',
         'HLA-DQA1*04:02-DQB1*06:39', 'HLA-DQA1*04:02-DQB1*06:40',
         'HLA-DQA1*04:02-DQB1*06:41', 'HLA-DQA1*04:02-DQB1*06:42', 'HLA-DQA1*04:02-DQB1*06:43', 'HLA-DQA1*04:02-DQB1*06:44',
         'HLA-DQA1*04:04-DQB1*02:01', 'HLA-DQA1*04:04-DQB1*02:02',
         'HLA-DQA1*04:04-DQB1*02:03', 'HLA-DQA1*04:04-DQB1*02:04', 'HLA-DQA1*04:04-DQB1*02:05', 'HLA-DQA1*04:04-DQB1*02:06',
         'HLA-DQA1*04:04-DQB1*03:01', 'HLA-DQA1*04:04-DQB1*03:02',
         'HLA-DQA1*04:04-DQB1*03:03', 'HLA-DQA1*04:04-DQB1*03:04', 'HLA-DQA1*04:04-DQB1*03:05', 'HLA-DQA1*04:04-DQB1*03:06',
         'HLA-DQA1*04:04-DQB1*03:07', 'HLA-DQA1*04:04-DQB1*03:08',
         'HLA-DQA1*04:04-DQB1*03:09', 'HLA-DQA1*04:04-DQB1*03:10', 'HLA-DQA1*04:04-DQB1*03:11', 'HLA-DQA1*04:04-DQB1*03:12',
         'HLA-DQA1*04:04-DQB1*03:13', 'HLA-DQA1*04:04-DQB1*03:14',
         'HLA-DQA1*04:04-DQB1*03:15', 'HLA-DQA1*04:04-DQB1*03:16', 'HLA-DQA1*04:04-DQB1*03:17', 'HLA-DQA1*04:04-DQB1*03:18',
         'HLA-DQA1*04:04-DQB1*03:19', 'HLA-DQA1*04:04-DQB1*03:20',
         'HLA-DQA1*04:04-DQB1*03:21', 'HLA-DQA1*04:04-DQB1*03:22', 'HLA-DQA1*04:04-DQB1*03:23', 'HLA-DQA1*04:04-DQB1*03:24',
         'HLA-DQA1*04:04-DQB1*03:25', 'HLA-DQA1*04:04-DQB1*03:26',
         'HLA-DQA1*04:04-DQB1*03:27', 'HLA-DQA1*04:04-DQB1*03:28', 'HLA-DQA1*04:04-DQB1*03:29', 'HLA-DQA1*04:04-DQB1*03:30',
         'HLA-DQA1*04:04-DQB1*03:31', 'HLA-DQA1*04:04-DQB1*03:32',
         'HLA-DQA1*04:04-DQB1*03:33', 'HLA-DQA1*04:04-DQB1*03:34', 'HLA-DQA1*04:04-DQB1*03:35', 'HLA-DQA1*04:04-DQB1*03:36',
         'HLA-DQA1*04:04-DQB1*03:37', 'HLA-DQA1*04:04-DQB1*03:38',
         'HLA-DQA1*04:04-DQB1*04:01', 'HLA-DQA1*04:04-DQB1*04:02', 'HLA-DQA1*04:04-DQB1*04:03', 'HLA-DQA1*04:04-DQB1*04:04',
         'HLA-DQA1*04:04-DQB1*04:05', 'HLA-DQA1*04:04-DQB1*04:06',
         'HLA-DQA1*04:04-DQB1*04:07', 'HLA-DQA1*04:04-DQB1*04:08', 'HLA-DQA1*04:04-DQB1*05:01', 'HLA-DQA1*04:04-DQB1*05:02',
         'HLA-DQA1*04:04-DQB1*05:03', 'HLA-DQA1*04:04-DQB1*05:05',
         'HLA-DQA1*04:04-DQB1*05:06', 'HLA-DQA1*04:04-DQB1*05:07', 'HLA-DQA1*04:04-DQB1*05:08', 'HLA-DQA1*04:04-DQB1*05:09',
         'HLA-DQA1*04:04-DQB1*05:10', 'HLA-DQA1*04:04-DQB1*05:11',
         'HLA-DQA1*04:04-DQB1*05:12', 'HLA-DQA1*04:04-DQB1*05:13', 'HLA-DQA1*04:04-DQB1*05:14', 'HLA-DQA1*04:04-DQB1*06:01',
         'HLA-DQA1*04:04-DQB1*06:02', 'HLA-DQA1*04:04-DQB1*06:03',
         'HLA-DQA1*04:04-DQB1*06:04', 'HLA-DQA1*04:04-DQB1*06:07', 'HLA-DQA1*04:04-DQB1*06:08', 'HLA-DQA1*04:04-DQB1*06:09',
         'HLA-DQA1*04:04-DQB1*06:10', 'HLA-DQA1*04:04-DQB1*06:11',
         'HLA-DQA1*04:04-DQB1*06:12', 'HLA-DQA1*04:04-DQB1*06:14', 'HLA-DQA1*04:04-DQB1*06:15', 'HLA-DQA1*04:04-DQB1*06:16',
         'HLA-DQA1*04:04-DQB1*06:17', 'HLA-DQA1*04:04-DQB1*06:18',
         'HLA-DQA1*04:04-DQB1*06:19', 'HLA-DQA1*04:04-DQB1*06:21', 'HLA-DQA1*04:04-DQB1*06:22', 'HLA-DQA1*04:04-DQB1*06:23',
         'HLA-DQA1*04:04-DQB1*06:24', 'HLA-DQA1*04:04-DQB1*06:25',
         'HLA-DQA1*04:04-DQB1*06:27', 'HLA-DQA1*04:04-DQB1*06:28', 'HLA-DQA1*04:04-DQB1*06:29', 'HLA-DQA1*04:04-DQB1*06:30',
         'HLA-DQA1*04:04-DQB1*06:31', 'HLA-DQA1*04:04-DQB1*06:32',
         'HLA-DQA1*04:04-DQB1*06:33', 'HLA-DQA1*04:04-DQB1*06:34', 'HLA-DQA1*04:04-DQB1*06:35', 'HLA-DQA1*04:04-DQB1*06:36',
         'HLA-DQA1*04:04-DQB1*06:37', 'HLA-DQA1*04:04-DQB1*06:38',
         'HLA-DQA1*04:04-DQB1*06:39', 'HLA-DQA1*04:04-DQB1*06:40', 'HLA-DQA1*04:04-DQB1*06:41', 'HLA-DQA1*04:04-DQB1*06:42',
         'HLA-DQA1*04:04-DQB1*06:43', 'HLA-DQA1*04:04-DQB1*06:44',
         'HLA-DQA1*05:01-DQB1*02:01', 'HLA-DQA1*05:01-DQB1*02:02', 'HLA-DQA1*05:01-DQB1*02:03', 'HLA-DQA1*05:01-DQB1*02:04',
         'HLA-DQA1*05:01-DQB1*02:05', 'HLA-DQA1*05:01-DQB1*02:06',
         'HLA-DQA1*05:01-DQB1*03:01', 'HLA-DQA1*05:01-DQB1*03:02', 'HLA-DQA1*05:01-DQB1*03:03', 'HLA-DQA1*05:01-DQB1*03:04',
         'HLA-DQA1*05:01-DQB1*03:05', 'HLA-DQA1*05:01-DQB1*03:06',
         'HLA-DQA1*05:01-DQB1*03:07', 'HLA-DQA1*05:01-DQB1*03:08', 'HLA-DQA1*05:01-DQB1*03:09', 'HLA-DQA1*05:01-DQB1*03:10',
         'HLA-DQA1*05:01-DQB1*03:11', 'HLA-DQA1*05:01-DQB1*03:12',
         'HLA-DQA1*05:01-DQB1*03:13', 'HLA-DQA1*05:01-DQB1*03:14', 'HLA-DQA1*05:01-DQB1*03:15', 'HLA-DQA1*05:01-DQB1*03:16',
         'HLA-DQA1*05:01-DQB1*03:17', 'HLA-DQA1*05:01-DQB1*03:18',
         'HLA-DQA1*05:01-DQB1*03:19', 'HLA-DQA1*05:01-DQB1*03:20', 'HLA-DQA1*05:01-DQB1*03:21', 'HLA-DQA1*05:01-DQB1*03:22',
         'HLA-DQA1*05:01-DQB1*03:23', 'HLA-DQA1*05:01-DQB1*03:24',
         'HLA-DQA1*05:01-DQB1*03:25', 'HLA-DQA1*05:01-DQB1*03:26', 'HLA-DQA1*05:01-DQB1*03:27', 'HLA-DQA1*05:01-DQB1*03:28',
         'HLA-DQA1*05:01-DQB1*03:29', 'HLA-DQA1*05:01-DQB1*03:30',
         'HLA-DQA1*05:01-DQB1*03:31', 'HLA-DQA1*05:01-DQB1*03:32', 'HLA-DQA1*05:01-DQB1*03:33', 'HLA-DQA1*05:01-DQB1*03:34',
         'HLA-DQA1*05:01-DQB1*03:35', 'HLA-DQA1*05:01-DQB1*03:36',
         'HLA-DQA1*05:01-DQB1*03:37', 'HLA-DQA1*05:01-DQB1*03:38', 'HLA-DQA1*05:01-DQB1*04:01', 'HLA-DQA1*05:01-DQB1*04:02',
         'HLA-DQA1*05:01-DQB1*04:03', 'HLA-DQA1*05:01-DQB1*04:04',
         'HLA-DQA1*05:01-DQB1*04:05', 'HLA-DQA1*05:01-DQB1*04:06', 'HLA-DQA1*05:01-DQB1*04:07', 'HLA-DQA1*05:01-DQB1*04:08',
         'HLA-DQA1*05:01-DQB1*05:01', 'HLA-DQA1*05:01-DQB1*05:02',
         'HLA-DQA1*05:01-DQB1*05:03', 'HLA-DQA1*05:01-DQB1*05:05', 'HLA-DQA1*05:01-DQB1*05:06', 'HLA-DQA1*05:01-DQB1*05:07',
         'HLA-DQA1*05:01-DQB1*05:08', 'HLA-DQA1*05:01-DQB1*05:09',
         'HLA-DQA1*05:01-DQB1*05:10', 'HLA-DQA1*05:01-DQB1*05:11', 'HLA-DQA1*05:01-DQB1*05:12', 'HLA-DQA1*05:01-DQB1*05:13',
         'HLA-DQA1*05:01-DQB1*05:14', 'HLA-DQA1*05:01-DQB1*06:01',
         'HLA-DQA1*05:01-DQB1*06:02', 'HLA-DQA1*05:01-DQB1*06:03', 'HLA-DQA1*05:01-DQB1*06:04', 'HLA-DQA1*05:01-DQB1*06:07',
         'HLA-DQA1*05:01-DQB1*06:08', 'HLA-DQA1*05:01-DQB1*06:09',
         'HLA-DQA1*05:01-DQB1*06:10', 'HLA-DQA1*05:01-DQB1*06:11', 'HLA-DQA1*05:01-DQB1*06:12', 'HLA-DQA1*05:01-DQB1*06:14',
         'HLA-DQA1*05:01-DQB1*06:15', 'HLA-DQA1*05:01-DQB1*06:16',
         'HLA-DQA1*05:01-DQB1*06:17', 'HLA-DQA1*05:01-DQB1*06:18', 'HLA-DQA1*05:01-DQB1*06:19', 'HLA-DQA1*05:01-DQB1*06:21',
         'HLA-DQA1*05:01-DQB1*06:22', 'HLA-DQA1*05:01-DQB1*06:23',
         'HLA-DQA1*05:01-DQB1*06:24', 'HLA-DQA1*05:01-DQB1*06:25', 'HLA-DQA1*05:01-DQB1*06:27', 'HLA-DQA1*05:01-DQB1*06:28',
         'HLA-DQA1*05:01-DQB1*06:29', 'HLA-DQA1*05:01-DQB1*06:30',
         'HLA-DQA1*05:01-DQB1*06:31', 'HLA-DQA1*05:01-DQB1*06:32', 'HLA-DQA1*05:01-DQB1*06:33', 'HLA-DQA1*05:01-DQB1*06:34',
         'HLA-DQA1*05:01-DQB1*06:35', 'HLA-DQA1*05:01-DQB1*06:36',
         'HLA-DQA1*05:01-DQB1*06:37', 'HLA-DQA1*05:01-DQB1*06:38', 'HLA-DQA1*05:01-DQB1*06:39', 'HLA-DQA1*05:01-DQB1*06:40',
         'HLA-DQA1*05:01-DQB1*06:41', 'HLA-DQA1*05:01-DQB1*06:42',
         'HLA-DQA1*05:01-DQB1*06:43', 'HLA-DQA1*05:01-DQB1*06:44', 'HLA-DQA1*05:03-DQB1*02:01', 'HLA-DQA1*05:03-DQB1*02:02',
         'HLA-DQA1*05:03-DQB1*02:03', 'HLA-DQA1*05:03-DQB1*02:04',
         'HLA-DQA1*05:03-DQB1*02:05', 'HLA-DQA1*05:03-DQB1*02:06', 'HLA-DQA1*05:03-DQB1*03:01', 'HLA-DQA1*05:03-DQB1*03:02',
         'HLA-DQA1*05:03-DQB1*03:03', 'HLA-DQA1*05:03-DQB1*03:04',
         'HLA-DQA1*05:03-DQB1*03:05', 'HLA-DQA1*05:03-DQB1*03:06', 'HLA-DQA1*05:03-DQB1*03:07', 'HLA-DQA1*05:03-DQB1*03:08',
         'HLA-DQA1*05:03-DQB1*03:09', 'HLA-DQA1*05:03-DQB1*03:10',
         'HLA-DQA1*05:03-DQB1*03:11', 'HLA-DQA1*05:03-DQB1*03:12', 'HLA-DQA1*05:03-DQB1*03:13', 'HLA-DQA1*05:03-DQB1*03:14',
         'HLA-DQA1*05:03-DQB1*03:15', 'HLA-DQA1*05:03-DQB1*03:16',
         'HLA-DQA1*05:03-DQB1*03:17', 'HLA-DQA1*05:03-DQB1*03:18', 'HLA-DQA1*05:03-DQB1*03:19', 'HLA-DQA1*05:03-DQB1*03:20',
         'HLA-DQA1*05:03-DQB1*03:21', 'HLA-DQA1*05:03-DQB1*03:22',
         'HLA-DQA1*05:03-DQB1*03:23', 'HLA-DQA1*05:03-DQB1*03:24', 'HLA-DQA1*05:03-DQB1*03:25', 'HLA-DQA1*05:03-DQB1*03:26',
         'HLA-DQA1*05:03-DQB1*03:27', 'HLA-DQA1*05:03-DQB1*03:28',
         'HLA-DQA1*05:03-DQB1*03:29', 'HLA-DQA1*05:03-DQB1*03:30', 'HLA-DQA1*05:03-DQB1*03:31', 'HLA-DQA1*05:03-DQB1*03:32',
         'HLA-DQA1*05:03-DQB1*03:33', 'HLA-DQA1*05:03-DQB1*03:34',
         'HLA-DQA1*05:03-DQB1*03:35', 'HLA-DQA1*05:03-DQB1*03:36', 'HLA-DQA1*05:03-DQB1*03:37', 'HLA-DQA1*05:03-DQB1*03:38',
         'HLA-DQA1*05:03-DQB1*04:01', 'HLA-DQA1*05:03-DQB1*04:02',
         'HLA-DQA1*05:03-DQB1*04:03', 'HLA-DQA1*05:03-DQB1*04:04', 'HLA-DQA1*05:03-DQB1*04:05', 'HLA-DQA1*05:03-DQB1*04:06',
         'HLA-DQA1*05:03-DQB1*04:07', 'HLA-DQA1*05:03-DQB1*04:08',
         'HLA-DQA1*05:03-DQB1*05:01', 'HLA-DQA1*05:03-DQB1*05:02', 'HLA-DQA1*05:03-DQB1*05:03', 'HLA-DQA1*05:03-DQB1*05:05',
         'HLA-DQA1*05:03-DQB1*05:06', 'HLA-DQA1*05:03-DQB1*05:07',
         'HLA-DQA1*05:03-DQB1*05:08', 'HLA-DQA1*05:03-DQB1*05:09', 'HLA-DQA1*05:03-DQB1*05:10', 'HLA-DQA1*05:03-DQB1*05:11',
         'HLA-DQA1*05:03-DQB1*05:12', 'HLA-DQA1*05:03-DQB1*05:13',
         'HLA-DQA1*05:03-DQB1*05:14', 'HLA-DQA1*05:03-DQB1*06:01', 'HLA-DQA1*05:03-DQB1*06:02', 'HLA-DQA1*05:03-DQB1*06:03',
         'HLA-DQA1*05:03-DQB1*06:04', 'HLA-DQA1*05:03-DQB1*06:07',
         'HLA-DQA1*05:03-DQB1*06:08', 'HLA-DQA1*05:03-DQB1*06:09', 'HLA-DQA1*05:03-DQB1*06:10', 'HLA-DQA1*05:03-DQB1*06:11',
         'HLA-DQA1*05:03-DQB1*06:12', 'HLA-DQA1*05:03-DQB1*06:14',
         'HLA-DQA1*05:03-DQB1*06:15', 'HLA-DQA1*05:03-DQB1*06:16', 'HLA-DQA1*05:03-DQB1*06:17', 'HLA-DQA1*05:03-DQB1*06:18',
         'HLA-DQA1*05:03-DQB1*06:19', 'HLA-DQA1*05:03-DQB1*06:21',
         'HLA-DQA1*05:03-DQB1*06:22', 'HLA-DQA1*05:03-DQB1*06:23', 'HLA-DQA1*05:03-DQB1*06:24', 'HLA-DQA1*05:03-DQB1*06:25',
         'HLA-DQA1*05:03-DQB1*06:27', 'HLA-DQA1*05:03-DQB1*06:28',
         'HLA-DQA1*05:03-DQB1*06:29', 'HLA-DQA1*05:03-DQB1*06:30', 'HLA-DQA1*05:03-DQB1*06:31', 'HLA-DQA1*05:03-DQB1*06:32',
         'HLA-DQA1*05:03-DQB1*06:33', 'HLA-DQA1*05:03-DQB1*06:34',
         'HLA-DQA1*05:03-DQB1*06:35', 'HLA-DQA1*05:03-DQB1*06:36', 'HLA-DQA1*05:03-DQB1*06:37', 'HLA-DQA1*05:03-DQB1*06:38',
         'HLA-DQA1*05:03-DQB1*06:39', 'HLA-DQA1*05:03-DQB1*06:40',
         'HLA-DQA1*05:03-DQB1*06:41', 'HLA-DQA1*05:03-DQB1*06:42', 'HLA-DQA1*05:03-DQB1*06:43', 'HLA-DQA1*05:03-DQB1*06:44',
         'HLA-DQA1*05:04-DQB1*02:01', 'HLA-DQA1*05:04-DQB1*02:02',
         'HLA-DQA1*05:04-DQB1*02:03', 'HLA-DQA1*05:04-DQB1*02:04', 'HLA-DQA1*05:04-DQB1*02:05', 'HLA-DQA1*05:04-DQB1*02:06',
         'HLA-DQA1*05:04-DQB1*03:01', 'HLA-DQA1*05:04-DQB1*03:02',
         'HLA-DQA1*05:04-DQB1*03:03', 'HLA-DQA1*05:04-DQB1*03:04', 'HLA-DQA1*05:04-DQB1*03:05', 'HLA-DQA1*05:04-DQB1*03:06',
         'HLA-DQA1*05:04-DQB1*03:07', 'HLA-DQA1*05:04-DQB1*03:08',
         'HLA-DQA1*05:04-DQB1*03:09', 'HLA-DQA1*05:04-DQB1*03:10', 'HLA-DQA1*05:04-DQB1*03:11', 'HLA-DQA1*05:04-DQB1*03:12',
         'HLA-DQA1*05:04-DQB1*03:13', 'HLA-DQA1*05:04-DQB1*03:14',
         'HLA-DQA1*05:04-DQB1*03:15', 'HLA-DQA1*05:04-DQB1*03:16', 'HLA-DQA1*05:04-DQB1*03:17', 'HLA-DQA1*05:04-DQB1*03:18',
         'HLA-DQA1*05:04-DQB1*03:19', 'HLA-DQA1*05:04-DQB1*03:20',
         'HLA-DQA1*05:04-DQB1*03:21', 'HLA-DQA1*05:04-DQB1*03:22', 'HLA-DQA1*05:04-DQB1*03:23', 'HLA-DQA1*05:04-DQB1*03:24',
         'HLA-DQA1*05:04-DQB1*03:25', 'HLA-DQA1*05:04-DQB1*03:26',
         'HLA-DQA1*05:04-DQB1*03:27', 'HLA-DQA1*05:04-DQB1*03:28', 'HLA-DQA1*05:04-DQB1*03:29', 'HLA-DQA1*05:04-DQB1*03:30',
         'HLA-DQA1*05:04-DQB1*03:31', 'HLA-DQA1*05:04-DQB1*03:32',
         'HLA-DQA1*05:04-DQB1*03:33', 'HLA-DQA1*05:04-DQB1*03:34', 'HLA-DQA1*05:04-DQB1*03:35', 'HLA-DQA1*05:04-DQB1*03:36',
         'HLA-DQA1*05:04-DQB1*03:37', 'HLA-DQA1*05:04-DQB1*03:38',
         'HLA-DQA1*05:04-DQB1*04:01', 'HLA-DQA1*05:04-DQB1*04:02', 'HLA-DQA1*05:04-DQB1*04:03', 'HLA-DQA1*05:04-DQB1*04:04',
         'HLA-DQA1*05:04-DQB1*04:05', 'HLA-DQA1*05:04-DQB1*04:06',
         'HLA-DQA1*05:04-DQB1*04:07', 'HLA-DQA1*05:04-DQB1*04:08', 'HLA-DQA1*05:04-DQB1*05:01', 'HLA-DQA1*05:04-DQB1*05:02',
         'HLA-DQA1*05:04-DQB1*05:03', 'HLA-DQA1*05:04-DQB1*05:05',
         'HLA-DQA1*05:04-DQB1*05:06', 'HLA-DQA1*05:04-DQB1*05:07', 'HLA-DQA1*05:04-DQB1*05:08', 'HLA-DQA1*05:04-DQB1*05:09',
         'HLA-DQA1*05:04-DQB1*05:10', 'HLA-DQA1*05:04-DQB1*05:11',
         'HLA-DQA1*05:04-DQB1*05:12', 'HLA-DQA1*05:04-DQB1*05:13', 'HLA-DQA1*05:04-DQB1*05:14', 'HLA-DQA1*05:04-DQB1*06:01',
         'HLA-DQA1*05:04-DQB1*06:02', 'HLA-DQA1*05:04-DQB1*06:03',
         'HLA-DQA1*05:04-DQB1*06:04', 'HLA-DQA1*05:04-DQB1*06:07', 'HLA-DQA1*05:04-DQB1*06:08', 'HLA-DQA1*05:04-DQB1*06:09',
         'HLA-DQA1*05:04-DQB1*06:10', 'HLA-DQA1*05:04-DQB1*06:11',
         'HLA-DQA1*05:04-DQB1*06:12', 'HLA-DQA1*05:04-DQB1*06:14', 'HLA-DQA1*05:04-DQB1*06:15', 'HLA-DQA1*05:04-DQB1*06:16',
         'HLA-DQA1*05:04-DQB1*06:17', 'HLA-DQA1*05:04-DQB1*06:18',
         'HLA-DQA1*05:04-DQB1*06:19', 'HLA-DQA1*05:04-DQB1*06:21', 'HLA-DQA1*05:04-DQB1*06:22', 'HLA-DQA1*05:04-DQB1*06:23',
         'HLA-DQA1*05:04-DQB1*06:24', 'HLA-DQA1*05:04-DQB1*06:25',
         'HLA-DQA1*05:04-DQB1*06:27', 'HLA-DQA1*05:04-DQB1*06:28', 'HLA-DQA1*05:04-DQB1*06:29', 'HLA-DQA1*05:04-DQB1*06:30',
         'HLA-DQA1*05:04-DQB1*06:31', 'HLA-DQA1*05:04-DQB1*06:32',
         'HLA-DQA1*05:04-DQB1*06:33', 'HLA-DQA1*05:04-DQB1*06:34', 'HLA-DQA1*05:04-DQB1*06:35', 'HLA-DQA1*05:04-DQB1*06:36',
         'HLA-DQA1*05:04-DQB1*06:37', 'HLA-DQA1*05:04-DQB1*06:38',
         'HLA-DQA1*05:04-DQB1*06:39', 'HLA-DQA1*05:04-DQB1*06:40', 'HLA-DQA1*05:04-DQB1*06:41', 'HLA-DQA1*05:04-DQB1*06:42',
         'HLA-DQA1*05:04-DQB1*06:43', 'HLA-DQA1*05:04-DQB1*06:44',
         'HLA-DQA1*05:05-DQB1*02:01', 'HLA-DQA1*05:05-DQB1*02:02', 'HLA-DQA1*05:05-DQB1*02:03', 'HLA-DQA1*05:05-DQB1*02:04',
         'HLA-DQA1*05:05-DQB1*02:05', 'HLA-DQA1*05:05-DQB1*02:06',
         'HLA-DQA1*05:05-DQB1*03:01', 'HLA-DQA1*05:05-DQB1*03:02', 'HLA-DQA1*05:05-DQB1*03:03', 'HLA-DQA1*05:05-DQB1*03:04',
         'HLA-DQA1*05:05-DQB1*03:05', 'HLA-DQA1*05:05-DQB1*03:06',
         'HLA-DQA1*05:05-DQB1*03:07', 'HLA-DQA1*05:05-DQB1*03:08', 'HLA-DQA1*05:05-DQB1*03:09', 'HLA-DQA1*05:05-DQB1*03:10',
         'HLA-DQA1*05:05-DQB1*03:11', 'HLA-DQA1*05:05-DQB1*03:12',
         'HLA-DQA1*05:05-DQB1*03:13', 'HLA-DQA1*05:05-DQB1*03:14', 'HLA-DQA1*05:05-DQB1*03:15', 'HLA-DQA1*05:05-DQB1*03:16',
         'HLA-DQA1*05:05-DQB1*03:17', 'HLA-DQA1*05:05-DQB1*03:18',
         'HLA-DQA1*05:05-DQB1*03:19', 'HLA-DQA1*05:05-DQB1*03:20', 'HLA-DQA1*05:05-DQB1*03:21', 'HLA-DQA1*05:05-DQB1*03:22',
         'HLA-DQA1*05:05-DQB1*03:23', 'HLA-DQA1*05:05-DQB1*03:24',
         'HLA-DQA1*05:05-DQB1*03:25', 'HLA-DQA1*05:05-DQB1*03:26', 'HLA-DQA1*05:05-DQB1*03:27', 'HLA-DQA1*05:05-DQB1*03:28',
         'HLA-DQA1*05:05-DQB1*03:29', 'HLA-DQA1*05:05-DQB1*03:30',
         'HLA-DQA1*05:05-DQB1*03:31', 'HLA-DQA1*05:05-DQB1*03:32', 'HLA-DQA1*05:05-DQB1*03:33', 'HLA-DQA1*05:05-DQB1*03:34',
         'HLA-DQA1*05:05-DQB1*03:35', 'HLA-DQA1*05:05-DQB1*03:36',
         'HLA-DQA1*05:05-DQB1*03:37', 'HLA-DQA1*05:05-DQB1*03:38', 'HLA-DQA1*05:05-DQB1*04:01', 'HLA-DQA1*05:05-DQB1*04:02',
         'HLA-DQA1*05:05-DQB1*04:03', 'HLA-DQA1*05:05-DQB1*04:04',
         'HLA-DQA1*05:05-DQB1*04:05', 'HLA-DQA1*05:05-DQB1*04:06', 'HLA-DQA1*05:05-DQB1*04:07', 'HLA-DQA1*05:05-DQB1*04:08',
         'HLA-DQA1*05:05-DQB1*05:01', 'HLA-DQA1*05:05-DQB1*05:02',
         'HLA-DQA1*05:05-DQB1*05:03', 'HLA-DQA1*05:05-DQB1*05:05', 'HLA-DQA1*05:05-DQB1*05:06', 'HLA-DQA1*05:05-DQB1*05:07',
         'HLA-DQA1*05:05-DQB1*05:08', 'HLA-DQA1*05:05-DQB1*05:09',
         'HLA-DQA1*05:05-DQB1*05:10', 'HLA-DQA1*05:05-DQB1*05:11', 'HLA-DQA1*05:05-DQB1*05:12', 'HLA-DQA1*05:05-DQB1*05:13',
         'HLA-DQA1*05:05-DQB1*05:14', 'HLA-DQA1*05:05-DQB1*06:01',
         'HLA-DQA1*05:05-DQB1*06:02', 'HLA-DQA1*05:05-DQB1*06:03', 'HLA-DQA1*05:05-DQB1*06:04', 'HLA-DQA1*05:05-DQB1*06:07',
         'HLA-DQA1*05:05-DQB1*06:08', 'HLA-DQA1*05:05-DQB1*06:09',
         'HLA-DQA1*05:05-DQB1*06:10', 'HLA-DQA1*05:05-DQB1*06:11', 'HLA-DQA1*05:05-DQB1*06:12', 'HLA-DQA1*05:05-DQB1*06:14',
         'HLA-DQA1*05:05-DQB1*06:15', 'HLA-DQA1*05:05-DQB1*06:16',
         'HLA-DQA1*05:05-DQB1*06:17', 'HLA-DQA1*05:05-DQB1*06:18', 'HLA-DQA1*05:05-DQB1*06:19', 'HLA-DQA1*05:05-DQB1*06:21',
         'HLA-DQA1*05:05-DQB1*06:22', 'HLA-DQA1*05:05-DQB1*06:23',
         'HLA-DQA1*05:05-DQB1*06:24', 'HLA-DQA1*05:05-DQB1*06:25', 'HLA-DQA1*05:05-DQB1*06:27', 'HLA-DQA1*05:05-DQB1*06:28',
         'HLA-DQA1*05:05-DQB1*06:29', 'HLA-DQA1*05:05-DQB1*06:30',
         'HLA-DQA1*05:05-DQB1*06:31', 'HLA-DQA1*05:05-DQB1*06:32', 'HLA-DQA1*05:05-DQB1*06:33', 'HLA-DQA1*05:05-DQB1*06:34',
         'HLA-DQA1*05:05-DQB1*06:35', 'HLA-DQA1*05:05-DQB1*06:36',
         'HLA-DQA1*05:05-DQB1*06:37', 'HLA-DQA1*05:05-DQB1*06:38', 'HLA-DQA1*05:05-DQB1*06:39', 'HLA-DQA1*05:05-DQB1*06:40',
         'HLA-DQA1*05:05-DQB1*06:41', 'HLA-DQA1*05:05-DQB1*06:42',
         'HLA-DQA1*05:05-DQB1*06:43', 'HLA-DQA1*05:05-DQB1*06:44', 'HLA-DQA1*05:06-DQB1*02:01', 'HLA-DQA1*05:06-DQB1*02:02',
         'HLA-DQA1*05:06-DQB1*02:03', 'HLA-DQA1*05:06-DQB1*02:04',
         'HLA-DQA1*05:06-DQB1*02:05', 'HLA-DQA1*05:06-DQB1*02:06', 'HLA-DQA1*05:06-DQB1*03:01', 'HLA-DQA1*05:06-DQB1*03:02',
         'HLA-DQA1*05:06-DQB1*03:03', 'HLA-DQA1*05:06-DQB1*03:04',
         'HLA-DQA1*05:06-DQB1*03:05', 'HLA-DQA1*05:06-DQB1*03:06', 'HLA-DQA1*05:06-DQB1*03:07', 'HLA-DQA1*05:06-DQB1*03:08',
         'HLA-DQA1*05:06-DQB1*03:09', 'HLA-DQA1*05:06-DQB1*03:10',
         'HLA-DQA1*05:06-DQB1*03:11', 'HLA-DQA1*05:06-DQB1*03:12', 'HLA-DQA1*05:06-DQB1*03:13', 'HLA-DQA1*05:06-DQB1*03:14',
         'HLA-DQA1*05:06-DQB1*03:15', 'HLA-DQA1*05:06-DQB1*03:16',
         'HLA-DQA1*05:06-DQB1*03:17', 'HLA-DQA1*05:06-DQB1*03:18', 'HLA-DQA1*05:06-DQB1*03:19', 'HLA-DQA1*05:06-DQB1*03:20',
         'HLA-DQA1*05:06-DQB1*03:21', 'HLA-DQA1*05:06-DQB1*03:22',
         'HLA-DQA1*05:06-DQB1*03:23', 'HLA-DQA1*05:06-DQB1*03:24', 'HLA-DQA1*05:06-DQB1*03:25', 'HLA-DQA1*05:06-DQB1*03:26',
         'HLA-DQA1*05:06-DQB1*03:27', 'HLA-DQA1*05:06-DQB1*03:28',
         'HLA-DQA1*05:06-DQB1*03:29', 'HLA-DQA1*05:06-DQB1*03:30', 'HLA-DQA1*05:06-DQB1*03:31', 'HLA-DQA1*05:06-DQB1*03:32',
         'HLA-DQA1*05:06-DQB1*03:33', 'HLA-DQA1*05:06-DQB1*03:34',
         'HLA-DQA1*05:06-DQB1*03:35', 'HLA-DQA1*05:06-DQB1*03:36', 'HLA-DQA1*05:06-DQB1*03:37', 'HLA-DQA1*05:06-DQB1*03:38',
         'HLA-DQA1*05:06-DQB1*04:01', 'HLA-DQA1*05:06-DQB1*04:02',
         'HLA-DQA1*05:06-DQB1*04:03', 'HLA-DQA1*05:06-DQB1*04:04', 'HLA-DQA1*05:06-DQB1*04:05', 'HLA-DQA1*05:06-DQB1*04:06',
         'HLA-DQA1*05:06-DQB1*04:07', 'HLA-DQA1*05:06-DQB1*04:08',
         'HLA-DQA1*05:06-DQB1*05:01', 'HLA-DQA1*05:06-DQB1*05:02', 'HLA-DQA1*05:06-DQB1*05:03', 'HLA-DQA1*05:06-DQB1*05:05',
         'HLA-DQA1*05:06-DQB1*05:06', 'HLA-DQA1*05:06-DQB1*05:07',
         'HLA-DQA1*05:06-DQB1*05:08', 'HLA-DQA1*05:06-DQB1*05:09', 'HLA-DQA1*05:06-DQB1*05:10', 'HLA-DQA1*05:06-DQB1*05:11',
         'HLA-DQA1*05:06-DQB1*05:12', 'HLA-DQA1*05:06-DQB1*05:13',
         'HLA-DQA1*05:06-DQB1*05:14', 'HLA-DQA1*05:06-DQB1*06:01', 'HLA-DQA1*05:06-DQB1*06:02', 'HLA-DQA1*05:06-DQB1*06:03',
         'HLA-DQA1*05:06-DQB1*06:04', 'HLA-DQA1*05:06-DQB1*06:07',
         'HLA-DQA1*05:06-DQB1*06:08', 'HLA-DQA1*05:06-DQB1*06:09', 'HLA-DQA1*05:06-DQB1*06:10', 'HLA-DQA1*05:06-DQB1*06:11',
         'HLA-DQA1*05:06-DQB1*06:12', 'HLA-DQA1*05:06-DQB1*06:14',
         'HLA-DQA1*05:06-DQB1*06:15', 'HLA-DQA1*05:06-DQB1*06:16', 'HLA-DQA1*05:06-DQB1*06:17', 'HLA-DQA1*05:06-DQB1*06:18',
         'HLA-DQA1*05:06-DQB1*06:19', 'HLA-DQA1*05:06-DQB1*06:21',
         'HLA-DQA1*05:06-DQB1*06:22', 'HLA-DQA1*05:06-DQB1*06:23', 'HLA-DQA1*05:06-DQB1*06:24', 'HLA-DQA1*05:06-DQB1*06:25',
         'HLA-DQA1*05:06-DQB1*06:27', 'HLA-DQA1*05:06-DQB1*06:28',
         'HLA-DQA1*05:06-DQB1*06:29', 'HLA-DQA1*05:06-DQB1*06:30', 'HLA-DQA1*05:06-DQB1*06:31', 'HLA-DQA1*05:06-DQB1*06:32',
         'HLA-DQA1*05:06-DQB1*06:33', 'HLA-DQA1*05:06-DQB1*06:34',
         'HLA-DQA1*05:06-DQB1*06:35', 'HLA-DQA1*05:06-DQB1*06:36', 'HLA-DQA1*05:06-DQB1*06:37', 'HLA-DQA1*05:06-DQB1*06:38',
         'HLA-DQA1*05:06-DQB1*06:39', 'HLA-DQA1*05:06-DQB1*06:40',
         'HLA-DQA1*05:06-DQB1*06:41', 'HLA-DQA1*05:06-DQB1*06:42', 'HLA-DQA1*05:06-DQB1*06:43', 'HLA-DQA1*05:06-DQB1*06:44',
         'HLA-DQA1*05:07-DQB1*02:01', 'HLA-DQA1*05:07-DQB1*02:02',
         'HLA-DQA1*05:07-DQB1*02:03', 'HLA-DQA1*05:07-DQB1*02:04', 'HLA-DQA1*05:07-DQB1*02:05', 'HLA-DQA1*05:07-DQB1*02:06',
         'HLA-DQA1*05:07-DQB1*03:01', 'HLA-DQA1*05:07-DQB1*03:02',
         'HLA-DQA1*05:07-DQB1*03:03', 'HLA-DQA1*05:07-DQB1*03:04', 'HLA-DQA1*05:07-DQB1*03:05', 'HLA-DQA1*05:07-DQB1*03:06',
         'HLA-DQA1*05:07-DQB1*03:07', 'HLA-DQA1*05:07-DQB1*03:08',
         'HLA-DQA1*05:07-DQB1*03:09', 'HLA-DQA1*05:07-DQB1*03:10', 'HLA-DQA1*05:07-DQB1*03:11', 'HLA-DQA1*05:07-DQB1*03:12',
         'HLA-DQA1*05:07-DQB1*03:13', 'HLA-DQA1*05:07-DQB1*03:14',
         'HLA-DQA1*05:07-DQB1*03:15', 'HLA-DQA1*05:07-DQB1*03:16', 'HLA-DQA1*05:07-DQB1*03:17', 'HLA-DQA1*05:07-DQB1*03:18',
         'HLA-DQA1*05:07-DQB1*03:19', 'HLA-DQA1*05:07-DQB1*03:20',
         'HLA-DQA1*05:07-DQB1*03:21', 'HLA-DQA1*05:07-DQB1*03:22', 'HLA-DQA1*05:07-DQB1*03:23', 'HLA-DQA1*05:07-DQB1*03:24',
         'HLA-DQA1*05:07-DQB1*03:25', 'HLA-DQA1*05:07-DQB1*03:26',
         'HLA-DQA1*05:07-DQB1*03:27', 'HLA-DQA1*05:07-DQB1*03:28', 'HLA-DQA1*05:07-DQB1*03:29', 'HLA-DQA1*05:07-DQB1*03:30',
         'HLA-DQA1*05:07-DQB1*03:31', 'HLA-DQA1*05:07-DQB1*03:32',
         'HLA-DQA1*05:07-DQB1*03:33', 'HLA-DQA1*05:07-DQB1*03:34', 'HLA-DQA1*05:07-DQB1*03:35', 'HLA-DQA1*05:07-DQB1*03:36',
         'HLA-DQA1*05:07-DQB1*03:37', 'HLA-DQA1*05:07-DQB1*03:38',
         'HLA-DQA1*05:07-DQB1*04:01', 'HLA-DQA1*05:07-DQB1*04:02', 'HLA-DQA1*05:07-DQB1*04:03', 'HLA-DQA1*05:07-DQB1*04:04',
         'HLA-DQA1*05:07-DQB1*04:05', 'HLA-DQA1*05:07-DQB1*04:06',
         'HLA-DQA1*05:07-DQB1*04:07', 'HLA-DQA1*05:07-DQB1*04:08', 'HLA-DQA1*05:07-DQB1*05:01', 'HLA-DQA1*05:07-DQB1*05:02',
         'HLA-DQA1*05:07-DQB1*05:03', 'HLA-DQA1*05:07-DQB1*05:05',
         'HLA-DQA1*05:07-DQB1*05:06', 'HLA-DQA1*05:07-DQB1*05:07', 'HLA-DQA1*05:07-DQB1*05:08', 'HLA-DQA1*05:07-DQB1*05:09',
         'HLA-DQA1*05:07-DQB1*05:10', 'HLA-DQA1*05:07-DQB1*05:11',
         'HLA-DQA1*05:07-DQB1*05:12', 'HLA-DQA1*05:07-DQB1*05:13', 'HLA-DQA1*05:07-DQB1*05:14', 'HLA-DQA1*05:07-DQB1*06:01',
         'HLA-DQA1*05:07-DQB1*06:02', 'HLA-DQA1*05:07-DQB1*06:03',
         'HLA-DQA1*05:07-DQB1*06:04', 'HLA-DQA1*05:07-DQB1*06:07', 'HLA-DQA1*05:07-DQB1*06:08', 'HLA-DQA1*05:07-DQB1*06:09',
         'HLA-DQA1*05:07-DQB1*06:10', 'HLA-DQA1*05:07-DQB1*06:11',
         'HLA-DQA1*05:07-DQB1*06:12', 'HLA-DQA1*05:07-DQB1*06:14', 'HLA-DQA1*05:07-DQB1*06:15', 'HLA-DQA1*05:07-DQB1*06:16',
         'HLA-DQA1*05:07-DQB1*06:17', 'HLA-DQA1*05:07-DQB1*06:18',
         'HLA-DQA1*05:07-DQB1*06:19', 'HLA-DQA1*05:07-DQB1*06:21', 'HLA-DQA1*05:07-DQB1*06:22', 'HLA-DQA1*05:07-DQB1*06:23',
         'HLA-DQA1*05:07-DQB1*06:24', 'HLA-DQA1*05:07-DQB1*06:25',
         'HLA-DQA1*05:07-DQB1*06:27', 'HLA-DQA1*05:07-DQB1*06:28', 'HLA-DQA1*05:07-DQB1*06:29', 'HLA-DQA1*05:07-DQB1*06:30',
         'HLA-DQA1*05:07-DQB1*06:31', 'HLA-DQA1*05:07-DQB1*06:32',
         'HLA-DQA1*05:07-DQB1*06:33', 'HLA-DQA1*05:07-DQB1*06:34', 'HLA-DQA1*05:07-DQB1*06:35', 'HLA-DQA1*05:07-DQB1*06:36',
         'HLA-DQA1*05:07-DQB1*06:37', 'HLA-DQA1*05:07-DQB1*06:38',
         'HLA-DQA1*05:07-DQB1*06:39', 'HLA-DQA1*05:07-DQB1*06:40', 'HLA-DQA1*05:07-DQB1*06:41', 'HLA-DQA1*05:07-DQB1*06:42',
         'HLA-DQA1*05:07-DQB1*06:43', 'HLA-DQA1*05:07-DQB1*06:44',
         'HLA-DQA1*05:08-DQB1*02:01', 'HLA-DQA1*05:08-DQB1*02:02', 'HLA-DQA1*05:08-DQB1*02:03', 'HLA-DQA1*05:08-DQB1*02:04',
         'HLA-DQA1*05:08-DQB1*02:05', 'HLA-DQA1*05:08-DQB1*02:06',
         'HLA-DQA1*05:08-DQB1*03:01', 'HLA-DQA1*05:08-DQB1*03:02', 'HLA-DQA1*05:08-DQB1*03:03', 'HLA-DQA1*05:08-DQB1*03:04',
         'HLA-DQA1*05:08-DQB1*03:05', 'HLA-DQA1*05:08-DQB1*03:06',
         'HLA-DQA1*05:08-DQB1*03:07', 'HLA-DQA1*05:08-DQB1*03:08', 'HLA-DQA1*05:08-DQB1*03:09', 'HLA-DQA1*05:08-DQB1*03:10',
         'HLA-DQA1*05:08-DQB1*03:11', 'HLA-DQA1*05:08-DQB1*03:12',
         'HLA-DQA1*05:08-DQB1*03:13', 'HLA-DQA1*05:08-DQB1*03:14', 'HLA-DQA1*05:08-DQB1*03:15', 'HLA-DQA1*05:08-DQB1*03:16',
         'HLA-DQA1*05:08-DQB1*03:17', 'HLA-DQA1*05:08-DQB1*03:18',
         'HLA-DQA1*05:08-DQB1*03:19', 'HLA-DQA1*05:08-DQB1*03:20', 'HLA-DQA1*05:08-DQB1*03:21', 'HLA-DQA1*05:08-DQB1*03:22',
         'HLA-DQA1*05:08-DQB1*03:23', 'HLA-DQA1*05:08-DQB1*03:24',
         'HLA-DQA1*05:08-DQB1*03:25', 'HLA-DQA1*05:08-DQB1*03:26', 'HLA-DQA1*05:08-DQB1*03:27', 'HLA-DQA1*05:08-DQB1*03:28',
         'HLA-DQA1*05:08-DQB1*03:29', 'HLA-DQA1*05:08-DQB1*03:30',
         'HLA-DQA1*05:08-DQB1*03:31', 'HLA-DQA1*05:08-DQB1*03:32', 'HLA-DQA1*05:08-DQB1*03:33', 'HLA-DQA1*05:08-DQB1*03:34',
         'HLA-DQA1*05:08-DQB1*03:35', 'HLA-DQA1*05:08-DQB1*03:36',
         'HLA-DQA1*05:08-DQB1*03:37', 'HLA-DQA1*05:08-DQB1*03:38', 'HLA-DQA1*05:08-DQB1*04:01', 'HLA-DQA1*05:08-DQB1*04:02',
         'HLA-DQA1*05:08-DQB1*04:03', 'HLA-DQA1*05:08-DQB1*04:04',
         'HLA-DQA1*05:08-DQB1*04:05', 'HLA-DQA1*05:08-DQB1*04:06', 'HLA-DQA1*05:08-DQB1*04:07', 'HLA-DQA1*05:08-DQB1*04:08',
         'HLA-DQA1*05:08-DQB1*05:01', 'HLA-DQA1*05:08-DQB1*05:02',
         'HLA-DQA1*05:08-DQB1*05:03', 'HLA-DQA1*05:08-DQB1*05:05', 'HLA-DQA1*05:08-DQB1*05:06', 'HLA-DQA1*05:08-DQB1*05:07',
         'HLA-DQA1*05:08-DQB1*05:08', 'HLA-DQA1*05:08-DQB1*05:09',
         'HLA-DQA1*05:08-DQB1*05:10', 'HLA-DQA1*05:08-DQB1*05:11', 'HLA-DQA1*05:08-DQB1*05:12', 'HLA-DQA1*05:08-DQB1*05:13',
         'HLA-DQA1*05:08-DQB1*05:14', 'HLA-DQA1*05:08-DQB1*06:01',
         'HLA-DQA1*05:08-DQB1*06:02', 'HLA-DQA1*05:08-DQB1*06:03', 'HLA-DQA1*05:08-DQB1*06:04', 'HLA-DQA1*05:08-DQB1*06:07',
         'HLA-DQA1*05:08-DQB1*06:08', 'HLA-DQA1*05:08-DQB1*06:09',
         'HLA-DQA1*05:08-DQB1*06:10', 'HLA-DQA1*05:08-DQB1*06:11', 'HLA-DQA1*05:08-DQB1*06:12', 'HLA-DQA1*05:08-DQB1*06:14',
         'HLA-DQA1*05:08-DQB1*06:15', 'HLA-DQA1*05:08-DQB1*06:16',
         'HLA-DQA1*05:08-DQB1*06:17', 'HLA-DQA1*05:08-DQB1*06:18', 'HLA-DQA1*05:08-DQB1*06:19', 'HLA-DQA1*05:08-DQB1*06:21',
         'HLA-DQA1*05:08-DQB1*06:22', 'HLA-DQA1*05:08-DQB1*06:23',
         'HLA-DQA1*05:08-DQB1*06:24', 'HLA-DQA1*05:08-DQB1*06:25', 'HLA-DQA1*05:08-DQB1*06:27', 'HLA-DQA1*05:08-DQB1*06:28',
         'HLA-DQA1*05:08-DQB1*06:29', 'HLA-DQA1*05:08-DQB1*06:30',
         'HLA-DQA1*05:08-DQB1*06:31', 'HLA-DQA1*05:08-DQB1*06:32', 'HLA-DQA1*05:08-DQB1*06:33', 'HLA-DQA1*05:08-DQB1*06:34',
         'HLA-DQA1*05:08-DQB1*06:35', 'HLA-DQA1*05:08-DQB1*06:36',
         'HLA-DQA1*05:08-DQB1*06:37', 'HLA-DQA1*05:08-DQB1*06:38', 'HLA-DQA1*05:08-DQB1*06:39', 'HLA-DQA1*05:08-DQB1*06:40',
         'HLA-DQA1*05:08-DQB1*06:41', 'HLA-DQA1*05:08-DQB1*06:42',
         'HLA-DQA1*05:08-DQB1*06:43', 'HLA-DQA1*05:08-DQB1*06:44', 'HLA-DQA1*05:09-DQB1*02:01', 'HLA-DQA1*05:09-DQB1*02:02',
         'HLA-DQA1*05:09-DQB1*02:03', 'HLA-DQA1*05:09-DQB1*02:04',
         'HLA-DQA1*05:09-DQB1*02:05', 'HLA-DQA1*05:09-DQB1*02:06', 'HLA-DQA1*05:09-DQB1*03:01', 'HLA-DQA1*05:09-DQB1*03:02',
         'HLA-DQA1*05:09-DQB1*03:03', 'HLA-DQA1*05:09-DQB1*03:04',
         'HLA-DQA1*05:09-DQB1*03:05', 'HLA-DQA1*05:09-DQB1*03:06', 'HLA-DQA1*05:09-DQB1*03:07', 'HLA-DQA1*05:09-DQB1*03:08',
         'HLA-DQA1*05:09-DQB1*03:09', 'HLA-DQA1*05:09-DQB1*03:10',
         'HLA-DQA1*05:09-DQB1*03:11', 'HLA-DQA1*05:09-DQB1*03:12', 'HLA-DQA1*05:09-DQB1*03:13', 'HLA-DQA1*05:09-DQB1*03:14',
         'HLA-DQA1*05:09-DQB1*03:15', 'HLA-DQA1*05:09-DQB1*03:16',
         'HLA-DQA1*05:09-DQB1*03:17', 'HLA-DQA1*05:09-DQB1*03:18', 'HLA-DQA1*05:09-DQB1*03:19', 'HLA-DQA1*05:09-DQB1*03:20',
         'HLA-DQA1*05:09-DQB1*03:21', 'HLA-DQA1*05:09-DQB1*03:22',
         'HLA-DQA1*05:09-DQB1*03:23', 'HLA-DQA1*05:09-DQB1*03:24', 'HLA-DQA1*05:09-DQB1*03:25', 'HLA-DQA1*05:09-DQB1*03:26',
         'HLA-DQA1*05:09-DQB1*03:27', 'HLA-DQA1*05:09-DQB1*03:28',
         'HLA-DQA1*05:09-DQB1*03:29', 'HLA-DQA1*05:09-DQB1*03:30', 'HLA-DQA1*05:09-DQB1*03:31', 'HLA-DQA1*05:09-DQB1*03:32',
         'HLA-DQA1*05:09-DQB1*03:33', 'HLA-DQA1*05:09-DQB1*03:34',
         'HLA-DQA1*05:09-DQB1*03:35', 'HLA-DQA1*05:09-DQB1*03:36', 'HLA-DQA1*05:09-DQB1*03:37', 'HLA-DQA1*05:09-DQB1*03:38',
         'HLA-DQA1*05:09-DQB1*04:01', 'HLA-DQA1*05:09-DQB1*04:02',
         'HLA-DQA1*05:09-DQB1*04:03', 'HLA-DQA1*05:09-DQB1*04:04', 'HLA-DQA1*05:09-DQB1*04:05', 'HLA-DQA1*05:09-DQB1*04:06',
         'HLA-DQA1*05:09-DQB1*04:07', 'HLA-DQA1*05:09-DQB1*04:08',
         'HLA-DQA1*05:09-DQB1*05:01', 'HLA-DQA1*05:09-DQB1*05:02', 'HLA-DQA1*05:09-DQB1*05:03', 'HLA-DQA1*05:09-DQB1*05:05',
         'HLA-DQA1*05:09-DQB1*05:06', 'HLA-DQA1*05:09-DQB1*05:07',
         'HLA-DQA1*05:09-DQB1*05:08', 'HLA-DQA1*05:09-DQB1*05:09', 'HLA-DQA1*05:09-DQB1*05:10', 'HLA-DQA1*05:09-DQB1*05:11',
         'HLA-DQA1*05:09-DQB1*05:12', 'HLA-DQA1*05:09-DQB1*05:13',
         'HLA-DQA1*05:09-DQB1*05:14', 'HLA-DQA1*05:09-DQB1*06:01', 'HLA-DQA1*05:09-DQB1*06:02', 'HLA-DQA1*05:09-DQB1*06:03',
         'HLA-DQA1*05:09-DQB1*06:04', 'HLA-DQA1*05:09-DQB1*06:07',
         'HLA-DQA1*05:09-DQB1*06:08', 'HLA-DQA1*05:09-DQB1*06:09', 'HLA-DQA1*05:09-DQB1*06:10', 'HLA-DQA1*05:09-DQB1*06:11',
         'HLA-DQA1*05:09-DQB1*06:12', 'HLA-DQA1*05:09-DQB1*06:14',
         'HLA-DQA1*05:09-DQB1*06:15', 'HLA-DQA1*05:09-DQB1*06:16', 'HLA-DQA1*05:09-DQB1*06:17', 'HLA-DQA1*05:09-DQB1*06:18',
         'HLA-DQA1*05:09-DQB1*06:19', 'HLA-DQA1*05:09-DQB1*06:21',
         'HLA-DQA1*05:09-DQB1*06:22', 'HLA-DQA1*05:09-DQB1*06:23', 'HLA-DQA1*05:09-DQB1*06:24', 'HLA-DQA1*05:09-DQB1*06:25',
         'HLA-DQA1*05:09-DQB1*06:27', 'HLA-DQA1*05:09-DQB1*06:28',
         'HLA-DQA1*05:09-DQB1*06:29', 'HLA-DQA1*05:09-DQB1*06:30', 'HLA-DQA1*05:09-DQB1*06:31', 'HLA-DQA1*05:09-DQB1*06:32',
         'HLA-DQA1*05:09-DQB1*06:33', 'HLA-DQA1*05:09-DQB1*06:34',
         'HLA-DQA1*05:09-DQB1*06:35', 'HLA-DQA1*05:09-DQB1*06:36', 'HLA-DQA1*05:09-DQB1*06:37', 'HLA-DQA1*05:09-DQB1*06:38',
         'HLA-DQA1*05:09-DQB1*06:39', 'HLA-DQA1*05:09-DQB1*06:40',
         'HLA-DQA1*05:09-DQB1*06:41', 'HLA-DQA1*05:09-DQB1*06:42', 'HLA-DQA1*05:09-DQB1*06:43', 'HLA-DQA1*05:09-DQB1*06:44',
         'HLA-DQA1*05:10-DQB1*02:01', 'HLA-DQA1*05:10-DQB1*02:02',
         'HLA-DQA1*05:10-DQB1*02:03', 'HLA-DQA1*05:10-DQB1*02:04', 'HLA-DQA1*05:10-DQB1*02:05', 'HLA-DQA1*05:10-DQB1*02:06',
         'HLA-DQA1*05:10-DQB1*03:01', 'HLA-DQA1*05:10-DQB1*03:02',
         'HLA-DQA1*05:10-DQB1*03:03', 'HLA-DQA1*05:10-DQB1*03:04', 'HLA-DQA1*05:10-DQB1*03:05', 'HLA-DQA1*05:10-DQB1*03:06',
         'HLA-DQA1*05:10-DQB1*03:07', 'HLA-DQA1*05:10-DQB1*03:08',
         'HLA-DQA1*05:10-DQB1*03:09', 'HLA-DQA1*05:10-DQB1*03:10', 'HLA-DQA1*05:10-DQB1*03:11', 'HLA-DQA1*05:10-DQB1*03:12',
         'HLA-DQA1*05:10-DQB1*03:13', 'HLA-DQA1*05:10-DQB1*03:14',
         'HLA-DQA1*05:10-DQB1*03:15', 'HLA-DQA1*05:10-DQB1*03:16', 'HLA-DQA1*05:10-DQB1*03:17', 'HLA-DQA1*05:10-DQB1*03:18',
         'HLA-DQA1*05:10-DQB1*03:19', 'HLA-DQA1*05:10-DQB1*03:20',
         'HLA-DQA1*05:10-DQB1*03:21', 'HLA-DQA1*05:10-DQB1*03:22', 'HLA-DQA1*05:10-DQB1*03:23', 'HLA-DQA1*05:10-DQB1*03:24',
         'HLA-DQA1*05:10-DQB1*03:25', 'HLA-DQA1*05:10-DQB1*03:26',
         'HLA-DQA1*05:10-DQB1*03:27', 'HLA-DQA1*05:10-DQB1*03:28', 'HLA-DQA1*05:10-DQB1*03:29', 'HLA-DQA1*05:10-DQB1*03:30',
         'HLA-DQA1*05:10-DQB1*03:31', 'HLA-DQA1*05:10-DQB1*03:32',
         'HLA-DQA1*05:10-DQB1*03:33', 'HLA-DQA1*05:10-DQB1*03:34', 'HLA-DQA1*05:10-DQB1*03:35', 'HLA-DQA1*05:10-DQB1*03:36',
         'HLA-DQA1*05:10-DQB1*03:37', 'HLA-DQA1*05:10-DQB1*03:38',
         'HLA-DQA1*05:10-DQB1*04:01', 'HLA-DQA1*05:10-DQB1*04:02', 'HLA-DQA1*05:10-DQB1*04:03', 'HLA-DQA1*05:10-DQB1*04:04',
         'HLA-DQA1*05:10-DQB1*04:05', 'HLA-DQA1*05:10-DQB1*04:06',
         'HLA-DQA1*05:10-DQB1*04:07', 'HLA-DQA1*05:10-DQB1*04:08', 'HLA-DQA1*05:10-DQB1*05:01', 'HLA-DQA1*05:10-DQB1*05:02',
         'HLA-DQA1*05:10-DQB1*05:03', 'HLA-DQA1*05:10-DQB1*05:05',
         'HLA-DQA1*05:10-DQB1*05:06', 'HLA-DQA1*05:10-DQB1*05:07', 'HLA-DQA1*05:10-DQB1*05:08', 'HLA-DQA1*05:10-DQB1*05:09',
         'HLA-DQA1*05:10-DQB1*05:10', 'HLA-DQA1*05:10-DQB1*05:11',
         'HLA-DQA1*05:10-DQB1*05:12', 'HLA-DQA1*05:10-DQB1*05:13', 'HLA-DQA1*05:10-DQB1*05:14', 'HLA-DQA1*05:10-DQB1*06:01',
         'HLA-DQA1*05:10-DQB1*06:02', 'HLA-DQA1*05:10-DQB1*06:03',
         'HLA-DQA1*05:10-DQB1*06:04', 'HLA-DQA1*05:10-DQB1*06:07', 'HLA-DQA1*05:10-DQB1*06:08', 'HLA-DQA1*05:10-DQB1*06:09',
         'HLA-DQA1*05:10-DQB1*06:10', 'HLA-DQA1*05:10-DQB1*06:11',
         'HLA-DQA1*05:10-DQB1*06:12', 'HLA-DQA1*05:10-DQB1*06:14', 'HLA-DQA1*05:10-DQB1*06:15', 'HLA-DQA1*05:10-DQB1*06:16',
         'HLA-DQA1*05:10-DQB1*06:17', 'HLA-DQA1*05:10-DQB1*06:18',
         'HLA-DQA1*05:10-DQB1*06:19', 'HLA-DQA1*05:10-DQB1*06:21', 'HLA-DQA1*05:10-DQB1*06:22', 'HLA-DQA1*05:10-DQB1*06:23',
         'HLA-DQA1*05:10-DQB1*06:24', 'HLA-DQA1*05:10-DQB1*06:25',
         'HLA-DQA1*05:10-DQB1*06:27', 'HLA-DQA1*05:10-DQB1*06:28', 'HLA-DQA1*05:10-DQB1*06:29', 'HLA-DQA1*05:10-DQB1*06:30',
         'HLA-DQA1*05:10-DQB1*06:31', 'HLA-DQA1*05:10-DQB1*06:32',
         'HLA-DQA1*05:10-DQB1*06:33', 'HLA-DQA1*05:10-DQB1*06:34', 'HLA-DQA1*05:10-DQB1*06:35', 'HLA-DQA1*05:10-DQB1*06:36',
         'HLA-DQA1*05:10-DQB1*06:37', 'HLA-DQA1*05:10-DQB1*06:38',
         'HLA-DQA1*05:10-DQB1*06:39', 'HLA-DQA1*05:10-DQB1*06:40', 'HLA-DQA1*05:10-DQB1*06:41', 'HLA-DQA1*05:10-DQB1*06:42',
         'HLA-DQA1*05:10-DQB1*06:43', 'HLA-DQA1*05:10-DQB1*06:44',
         'HLA-DQA1*05:11-DQB1*02:01', 'HLA-DQA1*05:11-DQB1*02:02', 'HLA-DQA1*05:11-DQB1*02:03', 'HLA-DQA1*05:11-DQB1*02:04',
         'HLA-DQA1*05:11-DQB1*02:05', 'HLA-DQA1*05:11-DQB1*02:06',
         'HLA-DQA1*05:11-DQB1*03:01', 'HLA-DQA1*05:11-DQB1*03:02', 'HLA-DQA1*05:11-DQB1*03:03', 'HLA-DQA1*05:11-DQB1*03:04',
         'HLA-DQA1*05:11-DQB1*03:05', 'HLA-DQA1*05:11-DQB1*03:06',
         'HLA-DQA1*05:11-DQB1*03:07', 'HLA-DQA1*05:11-DQB1*03:08', 'HLA-DQA1*05:11-DQB1*03:09', 'HLA-DQA1*05:11-DQB1*03:10',
         'HLA-DQA1*05:11-DQB1*03:11', 'HLA-DQA1*05:11-DQB1*03:12',
         'HLA-DQA1*05:11-DQB1*03:13', 'HLA-DQA1*05:11-DQB1*03:14', 'HLA-DQA1*05:11-DQB1*03:15', 'HLA-DQA1*05:11-DQB1*03:16',
         'HLA-DQA1*05:11-DQB1*03:17', 'HLA-DQA1*05:11-DQB1*03:18',
         'HLA-DQA1*05:11-DQB1*03:19', 'HLA-DQA1*05:11-DQB1*03:20', 'HLA-DQA1*05:11-DQB1*03:21', 'HLA-DQA1*05:11-DQB1*03:22',
         'HLA-DQA1*05:11-DQB1*03:23', 'HLA-DQA1*05:11-DQB1*03:24',
         'HLA-DQA1*05:11-DQB1*03:25', 'HLA-DQA1*05:11-DQB1*03:26', 'HLA-DQA1*05:11-DQB1*03:27', 'HLA-DQA1*05:11-DQB1*03:28',
         'HLA-DQA1*05:11-DQB1*03:29', 'HLA-DQA1*05:11-DQB1*03:30',
         'HLA-DQA1*05:11-DQB1*03:31', 'HLA-DQA1*05:11-DQB1*03:32', 'HLA-DQA1*05:11-DQB1*03:33', 'HLA-DQA1*05:11-DQB1*03:34',
         'HLA-DQA1*05:11-DQB1*03:35', 'HLA-DQA1*05:11-DQB1*03:36',
         'HLA-DQA1*05:11-DQB1*03:37', 'HLA-DQA1*05:11-DQB1*03:38', 'HLA-DQA1*05:11-DQB1*04:01', 'HLA-DQA1*05:11-DQB1*04:02',
         'HLA-DQA1*05:11-DQB1*04:03', 'HLA-DQA1*05:11-DQB1*04:04',
         'HLA-DQA1*05:11-DQB1*04:05', 'HLA-DQA1*05:11-DQB1*04:06', 'HLA-DQA1*05:11-DQB1*04:07', 'HLA-DQA1*05:11-DQB1*04:08',
         'HLA-DQA1*05:11-DQB1*05:01', 'HLA-DQA1*05:11-DQB1*05:02',
         'HLA-DQA1*05:11-DQB1*05:03', 'HLA-DQA1*05:11-DQB1*05:05', 'HLA-DQA1*05:11-DQB1*05:06', 'HLA-DQA1*05:11-DQB1*05:07',
         'HLA-DQA1*05:11-DQB1*05:08', 'HLA-DQA1*05:11-DQB1*05:09',
         'HLA-DQA1*05:11-DQB1*05:10', 'HLA-DQA1*05:11-DQB1*05:11', 'HLA-DQA1*05:11-DQB1*05:12', 'HLA-DQA1*05:11-DQB1*05:13',
         'HLA-DQA1*05:11-DQB1*05:14', 'HLA-DQA1*05:11-DQB1*06:01',
         'HLA-DQA1*05:11-DQB1*06:02', 'HLA-DQA1*05:11-DQB1*06:03', 'HLA-DQA1*05:11-DQB1*06:04', 'HLA-DQA1*05:11-DQB1*06:07',
         'HLA-DQA1*05:11-DQB1*06:08', 'HLA-DQA1*05:11-DQB1*06:09',
         'HLA-DQA1*05:11-DQB1*06:10', 'HLA-DQA1*05:11-DQB1*06:11', 'HLA-DQA1*05:11-DQB1*06:12', 'HLA-DQA1*05:11-DQB1*06:14',
         'HLA-DQA1*05:11-DQB1*06:15', 'HLA-DQA1*05:11-DQB1*06:16',
         'HLA-DQA1*05:11-DQB1*06:17', 'HLA-DQA1*05:11-DQB1*06:18', 'HLA-DQA1*05:11-DQB1*06:19', 'HLA-DQA1*05:11-DQB1*06:21',
         'HLA-DQA1*05:11-DQB1*06:22', 'HLA-DQA1*05:11-DQB1*06:23',
         'HLA-DQA1*05:11-DQB1*06:24', 'HLA-DQA1*05:11-DQB1*06:25', 'HLA-DQA1*05:11-DQB1*06:27', 'HLA-DQA1*05:11-DQB1*06:28',
         'HLA-DQA1*05:11-DQB1*06:29', 'HLA-DQA1*05:11-DQB1*06:30',
         'HLA-DQA1*05:11-DQB1*06:31', 'HLA-DQA1*05:11-DQB1*06:32', 'HLA-DQA1*05:11-DQB1*06:33', 'HLA-DQA1*05:11-DQB1*06:34',
         'HLA-DQA1*05:11-DQB1*06:35', 'HLA-DQA1*05:11-DQB1*06:36',
         'HLA-DQA1*05:11-DQB1*06:37', 'HLA-DQA1*05:11-DQB1*06:38', 'HLA-DQA1*05:11-DQB1*06:39', 'HLA-DQA1*05:11-DQB1*06:40',
         'HLA-DQA1*05:11-DQB1*06:41', 'HLA-DQA1*05:11-DQB1*06:42',
         'HLA-DQA1*05:11-DQB1*06:43', 'HLA-DQA1*05:11-DQB1*06:44', 'HLA-DQA1*06:01-DQB1*02:01', 'HLA-DQA1*06:01-DQB1*02:02',
         'HLA-DQA1*06:01-DQB1*02:03', 'HLA-DQA1*06:01-DQB1*02:04',
         'HLA-DQA1*06:01-DQB1*02:05', 'HLA-DQA1*06:01-DQB1*02:06', 'HLA-DQA1*06:01-DQB1*03:01', 'HLA-DQA1*06:01-DQB1*03:02',
         'HLA-DQA1*06:01-DQB1*03:03', 'HLA-DQA1*06:01-DQB1*03:04',
         'HLA-DQA1*06:01-DQB1*03:05', 'HLA-DQA1*06:01-DQB1*03:06', 'HLA-DQA1*06:01-DQB1*03:07', 'HLA-DQA1*06:01-DQB1*03:08',
         'HLA-DQA1*06:01-DQB1*03:09', 'HLA-DQA1*06:01-DQB1*03:10',
         'HLA-DQA1*06:01-DQB1*03:11', 'HLA-DQA1*06:01-DQB1*03:12', 'HLA-DQA1*06:01-DQB1*03:13', 'HLA-DQA1*06:01-DQB1*03:14',
         'HLA-DQA1*06:01-DQB1*03:15', 'HLA-DQA1*06:01-DQB1*03:16',
         'HLA-DQA1*06:01-DQB1*03:17', 'HLA-DQA1*06:01-DQB1*03:18', 'HLA-DQA1*06:01-DQB1*03:19', 'HLA-DQA1*06:01-DQB1*03:20',
         'HLA-DQA1*06:01-DQB1*03:21', 'HLA-DQA1*06:01-DQB1*03:22',
         'HLA-DQA1*06:01-DQB1*03:23', 'HLA-DQA1*06:01-DQB1*03:24', 'HLA-DQA1*06:01-DQB1*03:25', 'HLA-DQA1*06:01-DQB1*03:26',
         'HLA-DQA1*06:01-DQB1*03:27', 'HLA-DQA1*06:01-DQB1*03:28',
         'HLA-DQA1*06:01-DQB1*03:29', 'HLA-DQA1*06:01-DQB1*03:30', 'HLA-DQA1*06:01-DQB1*03:31', 'HLA-DQA1*06:01-DQB1*03:32',
         'HLA-DQA1*06:01-DQB1*03:33', 'HLA-DQA1*06:01-DQB1*03:34',
         'HLA-DQA1*06:01-DQB1*03:35', 'HLA-DQA1*06:01-DQB1*03:36', 'HLA-DQA1*06:01-DQB1*03:37', 'HLA-DQA1*06:01-DQB1*03:38',
         'HLA-DQA1*06:01-DQB1*04:01', 'HLA-DQA1*06:01-DQB1*04:02',
         'HLA-DQA1*06:01-DQB1*04:03', 'HLA-DQA1*06:01-DQB1*04:04', 'HLA-DQA1*06:01-DQB1*04:05', 'HLA-DQA1*06:01-DQB1*04:06',
         'HLA-DQA1*06:01-DQB1*04:07', 'HLA-DQA1*06:01-DQB1*04:08',
         'HLA-DQA1*06:01-DQB1*05:01', 'HLA-DQA1*06:01-DQB1*05:02', 'HLA-DQA1*06:01-DQB1*05:03', 'HLA-DQA1*06:01-DQB1*05:05',
         'HLA-DQA1*06:01-DQB1*05:06', 'HLA-DQA1*06:01-DQB1*05:07',
         'HLA-DQA1*06:01-DQB1*05:08', 'HLA-DQA1*06:01-DQB1*05:09', 'HLA-DQA1*06:01-DQB1*05:10', 'HLA-DQA1*06:01-DQB1*05:11',
         'HLA-DQA1*06:01-DQB1*05:12', 'HLA-DQA1*06:01-DQB1*05:13',
         'HLA-DQA1*06:01-DQB1*05:14', 'HLA-DQA1*06:01-DQB1*06:01', 'HLA-DQA1*06:01-DQB1*06:02', 'HLA-DQA1*06:01-DQB1*06:03',
         'HLA-DQA1*06:01-DQB1*06:04', 'HLA-DQA1*06:01-DQB1*06:07',
         'HLA-DQA1*06:01-DQB1*06:08', 'HLA-DQA1*06:01-DQB1*06:09', 'HLA-DQA1*06:01-DQB1*06:10', 'HLA-DQA1*06:01-DQB1*06:11',
         'HLA-DQA1*06:01-DQB1*06:12', 'HLA-DQA1*06:01-DQB1*06:14',
         'HLA-DQA1*06:01-DQB1*06:15', 'HLA-DQA1*06:01-DQB1*06:16', 'HLA-DQA1*06:01-DQB1*06:17', 'HLA-DQA1*06:01-DQB1*06:18',
         'HLA-DQA1*06:01-DQB1*06:19', 'HLA-DQA1*06:01-DQB1*06:21',
         'HLA-DQA1*06:01-DQB1*06:22', 'HLA-DQA1*06:01-DQB1*06:23', 'HLA-DQA1*06:01-DQB1*06:24', 'HLA-DQA1*06:01-DQB1*06:25',
         'HLA-DQA1*06:01-DQB1*06:27', 'HLA-DQA1*06:01-DQB1*06:28',
         'HLA-DQA1*06:01-DQB1*06:29', 'HLA-DQA1*06:01-DQB1*06:30', 'HLA-DQA1*06:01-DQB1*06:31', 'HLA-DQA1*06:01-DQB1*06:32',
         'HLA-DQA1*06:01-DQB1*06:33', 'HLA-DQA1*06:01-DQB1*06:34',
         'HLA-DQA1*06:01-DQB1*06:35', 'HLA-DQA1*06:01-DQB1*06:36', 'HLA-DQA1*06:01-DQB1*06:37', 'HLA-DQA1*06:01-DQB1*06:38',
         'HLA-DQA1*06:01-DQB1*06:39', 'HLA-DQA1*06:01-DQB1*06:40',
         'HLA-DQA1*06:01-DQB1*06:41', 'HLA-DQA1*06:01-DQB1*06:42', 'HLA-DQA1*06:01-DQB1*06:43', 'HLA-DQA1*06:01-DQB1*06:44',
         'HLA-DQA1*06:02-DQB1*02:01', 'HLA-DQA1*06:02-DQB1*02:02',
         'HLA-DQA1*06:02-DQB1*02:03', 'HLA-DQA1*06:02-DQB1*02:04', 'HLA-DQA1*06:02-DQB1*02:05', 'HLA-DQA1*06:02-DQB1*02:06',
         'HLA-DQA1*06:02-DQB1*03:01', 'HLA-DQA1*06:02-DQB1*03:02',
         'HLA-DQA1*06:02-DQB1*03:03', 'HLA-DQA1*06:02-DQB1*03:04', 'HLA-DQA1*06:02-DQB1*03:05', 'HLA-DQA1*06:02-DQB1*03:06',
         'HLA-DQA1*06:02-DQB1*03:07', 'HLA-DQA1*06:02-DQB1*03:08',
         'HLA-DQA1*06:02-DQB1*03:09', 'HLA-DQA1*06:02-DQB1*03:10', 'HLA-DQA1*06:02-DQB1*03:11', 'HLA-DQA1*06:02-DQB1*03:12',
         'HLA-DQA1*06:02-DQB1*03:13', 'HLA-DQA1*06:02-DQB1*03:14',
         'HLA-DQA1*06:02-DQB1*03:15', 'HLA-DQA1*06:02-DQB1*03:16', 'HLA-DQA1*06:02-DQB1*03:17', 'HLA-DQA1*06:02-DQB1*03:18',
         'HLA-DQA1*06:02-DQB1*03:19', 'HLA-DQA1*06:02-DQB1*03:20',
         'HLA-DQA1*06:02-DQB1*03:21', 'HLA-DQA1*06:02-DQB1*03:22', 'HLA-DQA1*06:02-DQB1*03:23', 'HLA-DQA1*06:02-DQB1*03:24',
         'HLA-DQA1*06:02-DQB1*03:25', 'HLA-DQA1*06:02-DQB1*03:26',
         'HLA-DQA1*06:02-DQB1*03:27', 'HLA-DQA1*06:02-DQB1*03:28', 'HLA-DQA1*06:02-DQB1*03:29', 'HLA-DQA1*06:02-DQB1*03:30',
         'HLA-DQA1*06:02-DQB1*03:31', 'HLA-DQA1*06:02-DQB1*03:32',
         'HLA-DQA1*06:02-DQB1*03:33', 'HLA-DQA1*06:02-DQB1*03:34', 'HLA-DQA1*06:02-DQB1*03:35', 'HLA-DQA1*06:02-DQB1*03:36',
         'HLA-DQA1*06:02-DQB1*03:37', 'HLA-DQA1*06:02-DQB1*03:38',
         'HLA-DQA1*06:02-DQB1*04:01', 'HLA-DQA1*06:02-DQB1*04:02', 'HLA-DQA1*06:02-DQB1*04:03', 'HLA-DQA1*06:02-DQB1*04:04',
         'HLA-DQA1*06:02-DQB1*04:05', 'HLA-DQA1*06:02-DQB1*04:06',
         'HLA-DQA1*06:02-DQB1*04:07', 'HLA-DQA1*06:02-DQB1*04:08', 'HLA-DQA1*06:02-DQB1*05:01', 'HLA-DQA1*06:02-DQB1*05:02',
         'HLA-DQA1*06:02-DQB1*05:03', 'HLA-DQA1*06:02-DQB1*05:05',
         'HLA-DQA1*06:02-DQB1*05:06', 'HLA-DQA1*06:02-DQB1*05:07', 'HLA-DQA1*06:02-DQB1*05:08', 'HLA-DQA1*06:02-DQB1*05:09',
         'HLA-DQA1*06:02-DQB1*05:10', 'HLA-DQA1*06:02-DQB1*05:11',
         'HLA-DQA1*06:02-DQB1*05:12', 'HLA-DQA1*06:02-DQB1*05:13', 'HLA-DQA1*06:02-DQB1*05:14', 'HLA-DQA1*06:02-DQB1*06:01',
         'HLA-DQA1*06:02-DQB1*06:02', 'HLA-DQA1*06:02-DQB1*06:03',
         'HLA-DQA1*06:02-DQB1*06:04', 'HLA-DQA1*06:02-DQB1*06:07', 'HLA-DQA1*06:02-DQB1*06:08', 'HLA-DQA1*06:02-DQB1*06:09',
         'HLA-DQA1*06:02-DQB1*06:10', 'HLA-DQA1*06:02-DQB1*06:11',
         'HLA-DQA1*06:02-DQB1*06:12', 'HLA-DQA1*06:02-DQB1*06:14', 'HLA-DQA1*06:02-DQB1*06:15', 'HLA-DQA1*06:02-DQB1*06:16',
         'HLA-DQA1*06:02-DQB1*06:17', 'HLA-DQA1*06:02-DQB1*06:18',
         'HLA-DQA1*06:02-DQB1*06:19', 'HLA-DQA1*06:02-DQB1*06:21', 'HLA-DQA1*06:02-DQB1*06:22', 'HLA-DQA1*06:02-DQB1*06:23',
         'HLA-DQA1*06:02-DQB1*06:24', 'HLA-DQA1*06:02-DQB1*06:25',
         'HLA-DQA1*06:02-DQB1*06:27', 'HLA-DQA1*06:02-DQB1*06:28', 'HLA-DQA1*06:02-DQB1*06:29', 'HLA-DQA1*06:02-DQB1*06:30',
         'HLA-DQA1*06:02-DQB1*06:31', 'HLA-DQA1*06:02-DQB1*06:32',
         'HLA-DQA1*06:02-DQB1*06:33', 'HLA-DQA1*06:02-DQB1*06:34', 'HLA-DQA1*06:02-DQB1*06:35', 'HLA-DQA1*06:02-DQB1*06:36',
         'HLA-DQA1*06:02-DQB1*06:37', 'HLA-DQA1*06:02-DQB1*06:38',
         'HLA-DQA1*06:02-DQB1*06:39', 'HLA-DQA1*06:02-DQB1*06:40', 'HLA-DQA1*06:02-DQB1*06:41', 'HLA-DQA1*06:02-DQB1*06:42',
         'HLA-DQA1*06:02-DQB1*06:43', 'HLA-DQA1*06:02-DQB1*06:44',
         'H-2-Iab', 'H-2-Iad'])
    __version = "3.0"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype.upper(), allele.subtype)
        elif isinstance(allele, CombinedAllele):
            return "HLA-%s%s%s-%s%s%s" % (allele.alpha_locus, allele.alpha_supertype, allele.alpha_subtype,
                                          allele.beta_locus, allele.beta_supertype, allele.beta_subtype)
        else:
            return "%s_%s%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        def parse_allele_from_external_result(alleles_str_out):
            """
            Parses allele string from external result to allele string representation of input

            :param str allele_str_out: The allele string representation from the external result output
            :return: str allele_str_in: The allele string representation from the external result input
            :rtype: str
            """
            alleles_str_in = []
            for allele_str_out in alleles_str_out:
                if allele_str_out.startswith('HLA-'):
                    allele_str_in = allele_str_out.replace('*','').replace(':','')
                elif allele_str_out.startswith('D'):
                    allele_str_in = allele_str_out.replace('*','_').replace(':','')
                else:
                    allele_str_in = allele_str_out
                alleles_str_in.append(allele_str_in)

            return(alleles_str_in)

        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in set([x for x in next(f) if x != ""])]
        # Convert output representation of allele to input representation of allele, because they differ
        alleles = parse_allele_from_external_result(alleles)
        
        next(f)
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCIIPAN_3_0]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCIIPAN_3_0 + i * Offset.NETMHCIIPAN_3_0])
                ranks[a][pep_seq] = float(row[RankIndex.NETMHCIIPAN_3_0 + i * Offset.NETMHCIIPAN_3_0])
                # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}

        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools
        and writes them to _file in the specific format

        No return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))


class NetMHCIIpan_3_1(NetMHCIIpan_3_0):
    """
    Implementation of NetMHCIIpan 3.1 adapter.

    .. note::

        Andreatta, M., Karosiene, E., Rasmussen, M., Stryhn, A., Buus, S., & Nielsen, M. (2015). Accurate pan-specific
        prediction of peptide-MHC class II binding affinity with improved binding core identification.
        Immunogenetics, 1-10.
    """

    __supported_length = frozenset([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    __name = "netmhcIIpan"
    __command = "netMHCIIpan -f {peptides} -inptype 1 -a {alleles} {options} -xls -xlsfile {out}"
    __alleles = frozenset(
        ['HLA-DRB1*01:01', 'HLA-DRB1*01:02', 'HLA-DRB1*01:03', 'HLA-DRB1*01:04', 'HLA-DRB1*01:05', 'HLA-DRB1*01:06',
         'HLA-DRB1*01:07', 'HLA-DRB1*01:08', 'HLA-DRB1*01:09', 'HLA-DRB1*01:10', 'HLA-DRB1*01:11', 'HLA-DRB1*01:12',
         'HLA-DRB1*01:13', 'HLA-DRB1*01:14', 'HLA-DRB1*01:15', 'HLA-DRB1*01:16', 'HLA-DRB1*01:17', 'HLA-DRB1*01:18',
         'HLA-DRB1*01:19', 'HLA-DRB1*01:20', 'HLA-DRB1*01:21', 'HLA-DRB1*01:22', 'HLA-DRB1*01:23', 'HLA-DRB1*01:24',
         'HLA-DRB1*01:25', 'HLA-DRB1*01:26', 'HLA-DRB1*01:27', 'HLA-DRB1*01:28', 'HLA-DRB1*01:29', 'HLA-DRB1*01:30',
         'HLA-DRB1*01:31', 'HLA-DRB1*01:32', 'HLA-DRB1*03:01', 'HLA-DRB1*03:02', 'HLA-DRB1*03:03', 'HLA-DRB1*03:04',
         'HLA-DRB1*03:05', 'HLA-DRB1*03:06', 'HLA-DRB1*03:07', 'HLA-DRB1*03:08', 'HLA-DRB1*03:10', 'HLA-DRB1*03:11',
         'HLA-DRB1*03:13', 'HLA-DRB1*03:14', 'HLA-DRB1*03:15', 'HLA-DRB1*03:17', 'HLA-DRB1*03:18', 'HLA-DRB1*03:19',
         'HLA-DRB1*03:20', 'HLA-DRB1*03:21', 'HLA-DRB1*03:22', 'HLA-DRB1*03:23', 'HLA-DRB1*03:24', 'HLA-DRB1*03:25',
         'HLA-DRB1*03:26', 'HLA-DRB1*03:27', 'HLA-DRB1*03:28', 'HLA-DRB1*03:29', 'HLA-DRB1*03:30', 'HLA-DRB1*03:31',
         'HLA-DRB1*03:32', 'HLA-DRB1*03:33', 'HLA-DRB1*03:34', 'HLA-DRB1*03:35', 'HLA-DRB1*03:36', 'HLA-DRB1*03:37',
         'HLA-DRB1*03:38', 'HLA-DRB1*03:39', 'HLA-DRB1*03:40', 'HLA-DRB1*03:41', 'HLA-DRB1*03:42', 'HLA-DRB1*03:43',
         'HLA-DRB1*03:44', 'HLA-DRB1*03:45', 'HLA-DRB1*03:46', 'HLA-DRB1*03:47', 'HLA-DRB1*03:48', 'HLA-DRB1*03:49',
         'HLA-DRB1*03:50', 'HLA-DRB1*03:51', 'HLA-DRB1*03:52', 'HLA-DRB1*03:53', 'HLA-DRB1*03:54', 'HLA-DRB1*03:55',
         'HLA-DRB1*04:01', 'HLA-DRB1*04:02', 'HLA-DRB1*04:03', 'HLA-DRB1*04:04', 'HLA-DRB1*04:05', 'HLA-DRB1*04:06',
         'HLA-DRB1*04:07', 'HLA-DRB1*04:08', 'HLA-DRB1*04:09', 'HLA-DRB1*04:10', 'HLA-DRB1*04:11', 'HLA-DRB1*04:12',
         'HLA-DRB1*04:13', 'HLA-DRB1*04:14', 'HLA-DRB1*04:15', 'HLA-DRB1*04:16', 'HLA-DRB1*04:17', 'HLA-DRB1*04:18',
         'HLA-DRB1*04:19', 'HLA-DRB1*04:21', 'HLA-DRB1*04:22', 'HLA-DRB1*04:23', 'HLA-DRB1*04:24', 'HLA-DRB1*04:26',
         'HLA-DRB1*04:27', 'HLA-DRB1*04:28', 'HLA-DRB1*04:29', 'HLA-DRB1*04:30', 'HLA-DRB1*04:31', 'HLA-DRB1*04:33',
         'HLA-DRB1*04:34', 'HLA-DRB1*04:35', 'HLA-DRB1*04:36', 'HLA-DRB1*04:37', 'HLA-DRB1*04:38', 'HLA-DRB1*04:39',
         'HLA-DRB1*04:40', 'HLA-DRB1*04:41', 'HLA-DRB1*04:42', 'HLA-DRB1*04:43', 'HLA-DRB1*04:44', 'HLA-DRB1*04:45',
         'HLA-DRB1*04:46', 'HLA-DRB1*04:47', 'HLA-DRB1*04:48', 'HLA-DRB1*04:49', 'HLA-DRB1*04:50', 'HLA-DRB1*04:51',
         'HLA-DRB1*04:52', 'HLA-DRB1*04:53', 'HLA-DRB1*04:54', 'HLA-DRB1*04:55', 'HLA-DRB1*04:56', 'HLA-DRB1*04:57',
         'HLA-DRB1*04:58', 'HLA-DRB1*04:59', 'HLA-DRB1*04:60', 'HLA-DRB1*04:61', 'HLA-DRB1*04:62', 'HLA-DRB1*04:63',
         'HLA-DRB1*04:64', 'HLA-DRB1*04:65', 'HLA-DRB1*04:66', 'HLA-DRB1*04:67', 'HLA-DRB1*04:68', 'HLA-DRB1*04:69',
         'HLA-DRB1*04:70', 'HLA-DRB1*04:71', 'HLA-DRB1*04:72', 'HLA-DRB1*04:73', 'HLA-DRB1*04:74', 'HLA-DRB1*04:75',
         'HLA-DRB1*04:76', 'HLA-DRB1*04:77', 'HLA-DRB1*04:78', 'HLA-DRB1*04:79', 'HLA-DRB1*04:80', 'HLA-DRB1*04:82',
         'HLA-DRB1*04:83', 'HLA-DRB1*04:84', 'HLA-DRB1*04:85', 'HLA-DRB1*04:86', 'HLA-DRB1*04:87', 'HLA-DRB1*04:88',
         'HLA-DRB1*04:89', 'HLA-DRB1*04:91', 'HLA-DRB1*07:01', 'HLA-DRB1*07:03', 'HLA-DRB1*07:04', 'HLA-DRB1*07:05',
         'HLA-DRB1*07:06', 'HLA-DRB1*07:07', 'HLA-DRB1*07:08', 'HLA-DRB1*07:09', 'HLA-DRB1*07:11', 'HLA-DRB1*07:12',
         'HLA-DRB1*07:13', 'HLA-DRB1*07:14', 'HLA-DRB1*07:15', 'HLA-DRB1*07:16', 'HLA-DRB1*07:17', 'HLA-DRB1*07:19',
         'HLA-DRB1*08:01', 'HLA-DRB1*08:02', 'HLA-DRB1*08:03', 'HLA-DRB1*08:04', 'HLA-DRB1*08:05', 'HLA-DRB1*08:06',
         'HLA-DRB1*08:07', 'HLA-DRB1*08:08', 'HLA-DRB1*08:09', 'HLA-DRB1*08:10', 'HLA-DRB1*08:11', 'HLA-DRB1*08:12',
         'HLA-DRB1*08:13', 'HLA-DRB1*08:14', 'HLA-DRB1*08:15', 'HLA-DRB1*08:16', 'HLA-DRB1*08:18', 'HLA-DRB1*08:19',
         'HLA-DRB1*08:20', 'HLA-DRB1*08:21', 'HLA-DRB1*08:22', 'HLA-DRB1*08:23', 'HLA-DRB1*08:24', 'HLA-DRB1*08:25',
         'HLA-DRB1*08:26', 'HLA-DRB1*08:27', 'HLA-DRB1*08:28', 'HLA-DRB1*08:29', 'HLA-DRB1*08:30', 'HLA-DRB1*08:31',
         'HLA-DRB1*08:32', 'HLA-DRB1*08:33', 'HLA-DRB1*08:34', 'HLA-DRB1*08:35', 'HLA-DRB1*08:36', 'HLA-DRB1*08:37',
         'HLA-DRB1*08:38', 'HLA-DRB1*08:39', 'HLA-DRB1*08:40', 'HLA-DRB1*09:01', 'HLA-DRB1*09:02', 'HLA-DRB1*09:03',
         'HLA-DRB1*09:04', 'HLA-DRB1*09:05', 'HLA-DRB1*09:06', 'HLA-DRB1*09:07', 'HLA-DRB1*09:08', 'HLA-DRB1*09:09',
         'HLA-DRB1*10:01', 'HLA-DRB1*10:02', 'HLA-DRB1*10:03', 'HLA-DRB1*11:01', 'HLA-DRB1*11:02', 'HLA-DRB1*11:03',
         'HLA-DRB1*11:04', 'HLA-DRB1*11:05', 'HLA-DRB1*11:06', 'HLA-DRB1*11:07', 'HLA-DRB1*11:08', 'HLA-DRB1*11:09',
         'HLA-DRB1*11:10', 'HLA-DRB1*11:11', 'HLA-DRB1*11:12', 'HLA-DRB1*11:13', 'HLA-DRB1*11:14', 'HLA-DRB1*11:15',
         'HLA-DRB1*11:16', 'HLA-DRB1*11:17', 'HLA-DRB1*11:18', 'HLA-DRB1*11:19', 'HLA-DRB1*11:20', 'HLA-DRB1*11:21',
         'HLA-DRB1*11:24', 'HLA-DRB1*11:25', 'HLA-DRB1*11:27', 'HLA-DRB1*11:28', 'HLA-DRB1*11:29', 'HLA-DRB1*11:30',
         'HLA-DRB1*11:31', 'HLA-DRB1*11:32', 'HLA-DRB1*11:33', 'HLA-DRB1*11:34', 'HLA-DRB1*11:35', 'HLA-DRB1*11:36',
         'HLA-DRB1*11:37', 'HLA-DRB1*11:38', 'HLA-DRB1*11:39', 'HLA-DRB1*11:41', 'HLA-DRB1*11:42', 'HLA-DRB1*11:43',
         'HLA-DRB1*11:44', 'HLA-DRB1*11:45', 'HLA-DRB1*11:46', 'HLA-DRB1*11:47', 'HLA-DRB1*11:48', 'HLA-DRB1*11:49',
         'HLA-DRB1*11:50', 'HLA-DRB1*11:51', 'HLA-DRB1*11:52', 'HLA-DRB1*11:53', 'HLA-DRB1*11:54', 'HLA-DRB1*11:55',
         'HLA-DRB1*11:56', 'HLA-DRB1*11:57', 'HLA-DRB1*11:58', 'HLA-DRB1*11:59', 'HLA-DRB1*11:60', 'HLA-DRB1*11:61',
         'HLA-DRB1*11:62', 'HLA-DRB1*11:63', 'HLA-DRB1*11:64', 'HLA-DRB1*11:65', 'HLA-DRB1*11:66', 'HLA-DRB1*11:67',
         'HLA-DRB1*11:68', 'HLA-DRB1*11:69', 'HLA-DRB1*11:70', 'HLA-DRB1*11:72', 'HLA-DRB1*11:73', 'HLA-DRB1*11:74',
         'HLA-DRB1*11:75', 'HLA-DRB1*11:76', 'HLA-DRB1*11:77', 'HLA-DRB1*11:78', 'HLA-DRB1*11:79', 'HLA-DRB1*11:80',
         'HLA-DRB1*11:81', 'HLA-DRB1*11:82', 'HLA-DRB1*11:83', 'HLA-DRB1*11:84', 'HLA-DRB1*11:85', 'HLA-DRB1*11:86',
         'HLA-DRB1*11:87', 'HLA-DRB1*11:88', 'HLA-DRB1*11:89', 'HLA-DRB1*11:90', 'HLA-DRB1*11:91', 'HLA-DRB1*11:92',
         'HLA-DRB1*11:93', 'HLA-DRB1*11:94', 'HLA-DRB1*11:95', 'HLA-DRB1*11:96', 'HLA-DRB1*12:01', 'HLA-DRB1*12:02',
         'HLA-DRB1*12:03', 'HLA-DRB1*12:04', 'HLA-DRB1*12:05', 'HLA-DRB1*12:06', 'HLA-DRB1*12:07', 'HLA-DRB1*12:08',
         'HLA-DRB1*12:09', 'HLA-DRB1*12:10', 'HLA-DRB1*12:11', 'HLA-DRB1*12:12', 'HLA-DRB1*12:13', 'HLA-DRB1*12:14',
         'HLA-DRB1*12:15', 'HLA-DRB1*12:16', 'HLA-DRB1*12:17', 'HLA-DRB1*12:18', 'HLA-DRB1*12:19', 'HLA-DRB1*12:20',
         'HLA-DRB1*12:21', 'HLA-DRB1*12:22', 'HLA-DRB1*12:23', 'HLA-DRB1*13:01', 'HLA-DRB1*13:02', 'HLA-DRB1*13:03',
         'HLA-DRB1*13:04', 'HLA-DRB1*13:05', 'HLA-DRB1*13:06', 'HLA-DRB1*13:07', 'HLA-DRB1*13:08', 'HLA-DRB1*13:09',
         'HLA-DRB1*13:10', 'HLA-DRB1*13:100', 'HLA-DRB1*13:101', 'HLA-DRB1*13:11', 'HLA-DRB1*13:12', 'HLA-DRB1*13:13',
         'HLA-DRB1*13:14', 'HLA-DRB1*13:15', 'HLA-DRB1*13:16', 'HLA-DRB1*13:17', 'HLA-DRB1*13:18', 'HLA-DRB1*13:19',
         'HLA-DRB1*13:20', 'HLA-DRB1*13:21', 'HLA-DRB1*13:22', 'HLA-DRB1*13:23', 'HLA-DRB1*13:24', 'HLA-DRB1*13:26',
         'HLA-DRB1*13:27', 'HLA-DRB1*13:29', 'HLA-DRB1*13:30', 'HLA-DRB1*13:31', 'HLA-DRB1*13:32', 'HLA-DRB1*13:33',
         'HLA-DRB1*13:34', 'HLA-DRB1*13:35', 'HLA-DRB1*13:36', 'HLA-DRB1*13:37', 'HLA-DRB1*13:38', 'HLA-DRB1*13:39',
         'HLA-DRB1*13:41', 'HLA-DRB1*13:42', 'HLA-DRB1*13:43', 'HLA-DRB1*13:44', 'HLA-DRB1*13:46', 'HLA-DRB1*13:47',
         'HLA-DRB1*13:48', 'HLA-DRB1*13:49', 'HLA-DRB1*13:50', 'HLA-DRB1*13:51', 'HLA-DRB1*13:52', 'HLA-DRB1*13:53',
         'HLA-DRB1*13:54', 'HLA-DRB1*13:55', 'HLA-DRB1*13:56', 'HLA-DRB1*13:57', 'HLA-DRB1*13:58', 'HLA-DRB1*13:59',
         'HLA-DRB1*13:60', 'HLA-DRB1*13:61', 'HLA-DRB1*13:62', 'HLA-DRB1*13:63', 'HLA-DRB1*13:64', 'HLA-DRB1*13:65',
         'HLA-DRB1*13:66', 'HLA-DRB1*13:67', 'HLA-DRB1*13:68', 'HLA-DRB1*13:69', 'HLA-DRB1*13:70', 'HLA-DRB1*13:71',
         'HLA-DRB1*13:72', 'HLA-DRB1*13:73', 'HLA-DRB1*13:74', 'HLA-DRB1*13:75', 'HLA-DRB1*13:76', 'HLA-DRB1*13:77',
         'HLA-DRB1*13:78', 'HLA-DRB1*13:79', 'HLA-DRB1*13:80', 'HLA-DRB1*13:81', 'HLA-DRB1*13:82', 'HLA-DRB1*13:83',
         'HLA-DRB1*13:84', 'HLA-DRB1*13:85', 'HLA-DRB1*13:86', 'HLA-DRB1*13:87', 'HLA-DRB1*13:88', 'HLA-DRB1*13:89',
         'HLA-DRB1*13:90', 'HLA-DRB1*13:91', 'HLA-DRB1*13:92', 'HLA-DRB1*13:93', 'HLA-DRB1*13:94', 'HLA-DRB1*13:95',
         'HLA-DRB1*13:96', 'HLA-DRB1*13:97', 'HLA-DRB1*13:98', 'HLA-DRB1*13:99', 'HLA-DRB1*14:01', 'HLA-DRB1*14:02',
         'HLA-DRB1*14:03', 'HLA-DRB1*14:04', 'HLA-DRB1*14:05', 'HLA-DRB1*14:06', 'HLA-DRB1*14:07', 'HLA-DRB1*14:08',
         'HLA-DRB1*14:09', 'HLA-DRB1*14:10', 'HLA-DRB1*14:11', 'HLA-DRB1*14:12', 'HLA-DRB1*14:13', 'HLA-DRB1*14:14',
         'HLA-DRB1*14:15', 'HLA-DRB1*14:16', 'HLA-DRB1*14:17', 'HLA-DRB1*14:18', 'HLA-DRB1*14:19', 'HLA-DRB1*14:20',
         'HLA-DRB1*14:21', 'HLA-DRB1*14:22', 'HLA-DRB1*14:23', 'HLA-DRB1*14:24', 'HLA-DRB1*14:25', 'HLA-DRB1*14:26',
         'HLA-DRB1*14:27', 'HLA-DRB1*14:28', 'HLA-DRB1*14:29', 'HLA-DRB1*14:30', 'HLA-DRB1*14:31', 'HLA-DRB1*14:32',
         'HLA-DRB1*14:33', 'HLA-DRB1*14:34', 'HLA-DRB1*14:35', 'HLA-DRB1*14:36', 'HLA-DRB1*14:37', 'HLA-DRB1*14:38',
         'HLA-DRB1*14:39', 'HLA-DRB1*14:40', 'HLA-DRB1*14:41', 'HLA-DRB1*14:42', 'HLA-DRB1*14:43', 'HLA-DRB1*14:44',
         'HLA-DRB1*14:45', 'HLA-DRB1*14:46', 'HLA-DRB1*14:47', 'HLA-DRB1*14:48', 'HLA-DRB1*14:49', 'HLA-DRB1*14:50',
         'HLA-DRB1*14:51', 'HLA-DRB1*14:52', 'HLA-DRB1*14:53', 'HLA-DRB1*14:54', 'HLA-DRB1*14:55', 'HLA-DRB1*14:56',
         'HLA-DRB1*14:57', 'HLA-DRB1*14:58', 'HLA-DRB1*14:59', 'HLA-DRB1*14:60', 'HLA-DRB1*14:61', 'HLA-DRB1*14:62',
         'HLA-DRB1*14:63', 'HLA-DRB1*14:64', 'HLA-DRB1*14:65', 'HLA-DRB1*14:67', 'HLA-DRB1*14:68', 'HLA-DRB1*14:69',
         'HLA-DRB1*14:70', 'HLA-DRB1*14:71', 'HLA-DRB1*14:72', 'HLA-DRB1*14:73', 'HLA-DRB1*14:74', 'HLA-DRB1*14:75',
         'HLA-DRB1*14:76', 'HLA-DRB1*14:77', 'HLA-DRB1*14:78', 'HLA-DRB1*14:79', 'HLA-DRB1*14:80', 'HLA-DRB1*14:81',
         'HLA-DRB1*14:82', 'HLA-DRB1*14:83', 'HLA-DRB1*14:84', 'HLA-DRB1*14:85', 'HLA-DRB1*14:86', 'HLA-DRB1*14:87',
         'HLA-DRB1*14:88', 'HLA-DRB1*14:89', 'HLA-DRB1*14:90', 'HLA-DRB1*14:91', 'HLA-DRB1*14:93', 'HLA-DRB1*14:94',
         'HLA-DRB1*14:95', 'HLA-DRB1*14:96', 'HLA-DRB1*14:97', 'HLA-DRB1*14:98', 'HLA-DRB1*14:99', 'HLA-DRB1*15:01',
         'HLA-DRB1*15:02', 'HLA-DRB1*15:03', 'HLA-DRB1*15:04', 'HLA-DRB1*15:05', 'HLA-DRB1*15:06', 'HLA-DRB1*15:07',
         'HLA-DRB1*15:08', 'HLA-DRB1*15:09', 'HLA-DRB1*15:10', 'HLA-DRB1*15:11', 'HLA-DRB1*15:12', 'HLA-DRB1*15:13',
         'HLA-DRB1*15:14', 'HLA-DRB1*15:15', 'HLA-DRB1*15:16', 'HLA-DRB1*15:18', 'HLA-DRB1*15:19', 'HLA-DRB1*15:20',
         'HLA-DRB1*15:21', 'HLA-DRB1*15:22', 'HLA-DRB1*15:23', 'HLA-DRB1*15:24', 'HLA-DRB1*15:25', 'HLA-DRB1*15:26',
         'HLA-DRB1*15:27', 'HLA-DRB1*15:28', 'HLA-DRB1*15:29', 'HLA-DRB1*15:30', 'HLA-DRB1*15:31', 'HLA-DRB1*15:32',
         'HLA-DRB1*15:33', 'HLA-DRB1*15:34', 'HLA-DRB1*15:35', 'HLA-DRB1*15:36', 'HLA-DRB1*15:37', 'HLA-DRB1*15:38',
         'HLA-DRB1*15:39', 'HLA-DRB1*15:40', 'HLA-DRB1*15:41', 'HLA-DRB1*15:42', 'HLA-DRB1*15:43', 'HLA-DRB1*15:44',
         'HLA-DRB1*15:45', 'HLA-DRB1*15:46', 'HLA-DRB1*15:47', 'HLA-DRB1*15:48', 'HLA-DRB1*15:49', 'HLA-DRB1*16:01',
         'HLA-DRB1*16:02', 'HLA-DRB1*16:03', 'HLA-DRB1*16:04', 'HLA-DRB1*16:05', 'HLA-DRB1*16:07', 'HLA-DRB1*16:08',
         'HLA-DRB1*16:09', 'HLA-DRB1*16:10', 'HLA-DRB1*16:11', 'HLA-DRB1*16:12', 'HLA-DRB1*16:14', 'HLA-DRB1*16:15',
         'HLA-DRB1*16:16', 'HLA-DRB3*01:01', 'HLA-DRB3*01:04', 'HLA-DRB3*01:05', 'HLA-DRB3*01:08', 'HLA-DRB3*01:09',
         'HLA-DRB3*01:11', 'HLA-DRB3*01:12', 'HLA-DRB3*01:13', 'HLA-DRB3*01:14', 'HLA-DRB3*02:01', 'HLA-DRB3*02:02',
         'HLA-DRB3*02:04', 'HLA-DRB3*02:05', 'HLA-DRB3*02:09', 'HLA-DRB3*02:10', 'HLA-DRB3*02:11', 'HLA-DRB3*02:12',
         'HLA-DRB3*02:13', 'HLA-DRB3*02:14', 'HLA-DRB3*02:15', 'HLA-DRB3*02:16', 'HLA-DRB3*02:17', 'HLA-DRB3*02:18',
         'HLA-DRB3*02:19', 'HLA-DRB3*02:20', 'HLA-DRB3*02:21', 'HLA-DRB3*02:22', 'HLA-DRB3*02:23', 'HLA-DRB3*02:24',
         'HLA-DRB3*02:25', 'HLA-DRB3*03:01', 'HLA-DRB3*03:03', 'HLA-DRB4*01:01', 'HLA-DRB4*01:03', 'HLA-DRB4*01:04',
         'HLA-DRB4*01:06', 'HLA-DRB4*01:07', 'HLA-DRB4*01:08', 'HLA-DRB5*01:01', 'HLA-DRB5*01:02', 'HLA-DRB5*01:03',
         'HLA-DRB5*01:04', 'HLA-DRB5*01:05', 'HLA-DRB5*01:06', 'HLA-DRB5*01:08N', 'HLA-DRB5*01:11', 'HLA-DRB5*01:12',
         'HLA-DRB5*01:13', 'HLA-DRB5*01:14', 'HLA-DRB5*02:02', 'HLA-DRB5*02:03', 'HLA-DRB5*02:04', 'HLA-DRB5*02:05',
         'HLA-DPA1*01:03-DPB1*01:01', 'HLA-DPA1*01:03-DPB1*02:01', 'HLA-DPA1*01:03-DPB1*02:02', 'HLA-DPA1*01:03-DPB1*03:01',
         'HLA-DPA1*01:03-DPB1*04:01', 'HLA-DPA1*01:03-DPB1*04:02', 'HLA-DPA1*01:03-DPB1*05:01', 'HLA-DPA1*01:03-DPB1*06:01',
         'HLA-DPA1*01:03-DPB1*08:01', 'HLA-DPA1*01:03-DPB1*09:01', 'HLA-DPA1*01:03-DPB1*10:001', 'HLA-DPA1*01:03-DPB1*10:01',
         'HLA-DPA1*01:03-DPB1*10:101', 'HLA-DPA1*01:03-DPB1*10:201',
         'HLA-DPA1*01:03-DPB1*10:301', 'HLA-DPA1*01:03-DPB1*10:401',
         'HLA-DPA1*01:03-DPB1*10:501', 'HLA-DPA1*01:03-DPB1*10:601', 'HLA-DPA1*01:03-DPB1*10:701', 'HLA-DPA1*01:03-DPB1*10:801',
         'HLA-DPA1*01:03-DPB1*10:901', 'HLA-DPA1*01:03-DPB1*11:001',
         'HLA-DPA1*01:03-DPB1*11:01', 'HLA-DPA1*01:03-DPB1*11:101', 'HLA-DPA1*01:03-DPB1*11:201', 'HLA-DPA1*01:03-DPB1*11:301',
         'HLA-DPA1*01:03-DPB1*11:401', 'HLA-DPA1*01:03-DPB1*11:501',
         'HLA-DPA1*01:03-DPB1*11:601', 'HLA-DPA1*01:03-DPB1*11:701', 'HLA-DPA1*01:03-DPB1*11:801', 'HLA-DPA1*01:03-DPB1*11:901',
         'HLA-DPA1*01:03-DPB1*12:101', 'HLA-DPA1*01:03-DPB1*12:201',
         'HLA-DPA1*01:03-DPB1*12:301', 'HLA-DPA1*01:03-DPB1*12:401', 'HLA-DPA1*01:03-DPB1*12:501', 'HLA-DPA1*01:03-DPB1*12:601',
         'HLA-DPA1*01:03-DPB1*12:701', 'HLA-DPA1*01:03-DPB1*12:801',
         'HLA-DPA1*01:03-DPB1*12:901', 'HLA-DPA1*01:03-DPB1*13:001', 'HLA-DPA1*01:03-DPB1*13:01', 'HLA-DPA1*01:03-DPB1*13:101',
         'HLA-DPA1*01:03-DPB1*13:201', 'HLA-DPA1*01:03-DPB1*13:301',
         'HLA-DPA1*01:03-DPB1*13:401', 'HLA-DPA1*01:03-DPB1*14:01', 'HLA-DPA1*01:03-DPB1*15:01', 'HLA-DPA1*01:03-DPB1*16:01',
         'HLA-DPA1*01:03-DPB1*17:01', 'HLA-DPA1*01:03-DPB1*18:01',
         'HLA-DPA1*01:03-DPB1*19:01', 'HLA-DPA1*01:03-DPB1*20:01', 'HLA-DPA1*01:03-DPB1*21:01', 'HLA-DPA1*01:03-DPB1*22:01',
         'HLA-DPA1*01:03-DPB1*23:01', 'HLA-DPA1*01:03-DPB1*24:01',
         'HLA-DPA1*01:03-DPB1*25:01', 'HLA-DPA1*01:03-DPB1*26:01', 'HLA-DPA1*01:03-DPB1*27:01', 'HLA-DPA1*01:03-DPB1*28:01',
         'HLA-DPA1*01:03-DPB1*29:01', 'HLA-DPA1*01:03-DPB1*30:01',
         'HLA-DPA1*01:03-DPB1*31:01', 'HLA-DPA1*01:03-DPB1*32:01', 'HLA-DPA1*01:03-DPB1*33:01', 'HLA-DPA1*01:03-DPB1*34:01',
         'HLA-DPA1*01:03-DPB1*35:01', 'HLA-DPA1*01:03-DPB1*36:01',
         'HLA-DPA1*01:03-DPB1*37:01', 'HLA-DPA1*01:03-DPB1*38:01', 'HLA-DPA1*01:03-DPB1*39:01', 'HLA-DPA1*01:03-DPB1*40:01',
         'HLA-DPA1*01:03-DPB1*41:01', 'HLA-DPA1*01:03-DPB1*44:01',
         'HLA-DPA1*01:03-DPB1*45:01', 'HLA-DPA1*01:03-DPB1*46:01', 'HLA-DPA1*01:03-DPB1*47:01', 'HLA-DPA1*01:03-DPB1*48:01',
         'HLA-DPA1*01:03-DPB1*49:01', 'HLA-DPA1*01:03-DPB1*50:01',
         'HLA-DPA1*01:03-DPB1*51:01', 'HLA-DPA1*01:03-DPB1*52:01', 'HLA-DPA1*01:03-DPB1*53:01', 'HLA-DPA1*01:03-DPB1*54:01',
         'HLA-DPA1*01:03-DPB1*55:01', 'HLA-DPA1*01:03-DPB1*56:01',
         'HLA-DPA1*01:03-DPB1*58:01', 'HLA-DPA1*01:03-DPB1*59:01', 'HLA-DPA1*01:03-DPB1*60:01', 'HLA-DPA1*01:03-DPB1*62:01',
         'HLA-DPA1*01:03-DPB1*63:01', 'HLA-DPA1*01:03-DPB1*65:01',
         'HLA-DPA1*01:03-DPB1*66:01', 'HLA-DPA1*01:03-DPB1*67:01', 'HLA-DPA1*01:03-DPB1*68:01', 'HLA-DPA1*01:03-DPB1*69:01',
         'HLA-DPA1*01:03-DPB1*70:01', 'HLA-DPA1*01:03-DPB1*71:01',
         'HLA-DPA1*01:03-DPB1*72:01', 'HLA-DPA1*01:03-DPB1*73:01', 'HLA-DPA1*01:03-DPB1*74:01', 'HLA-DPA1*01:03-DPB1*75:01',
         'HLA-DPA1*01:03-DPB1*76:01', 'HLA-DPA1*01:03-DPB1*77:01',
         'HLA-DPA1*01:03-DPB1*78:01', 'HLA-DPA1*01:03-DPB1*79:01', 'HLA-DPA1*01:03-DPB1*80:01', 'HLA-DPA1*01:03-DPB1*81:01',
         'HLA-DPA1*01:03-DPB1*82:01', 'HLA-DPA1*01:03-DPB1*83:01',
         'HLA-DPA1*01:03-DPB1*84:01', 'HLA-DPA1*01:03-DPB1*85:01', 'HLA-DPA1*01:03-DPB1*86:01', 'HLA-DPA1*01:03-DPB1*87:01',
         'HLA-DPA1*01:03-DPB1*88:01', 'HLA-DPA1*01:03-DPB1*89:01',
         'HLA-DPA1*01:03-DPB1*90:01', 'HLA-DPA1*01:03-DPB1*91:01', 'HLA-DPA1*01:03-DPB1*92:01', 'HLA-DPA1*01:03-DPB1*93:01',
         'HLA-DPA1*01:03-DPB1*94:01', 'HLA-DPA1*01:03-DPB1*95:01',
         'HLA-DPA1*01:03-DPB1*96:01', 'HLA-DPA1*01:03-DPB1*97:01', 'HLA-DPA1*01:03-DPB1*98:01', 'HLA-DPA1*01:03-DPB1*99:01',
         'HLA-DPA1*01:04-DPB1*01:01', 'HLA-DPA1*01:04-DPB1*02:01',
         'HLA-DPA1*01:04-DPB1*02:02', 'HLA-DPA1*01:04-DPB1*03:01', 'HLA-DPA1*01:04-DPB1*04:01', 'HLA-DPA1*01:04-DPB1*04:02',
         'HLA-DPA1*01:04-DPB1*05:01', 'HLA-DPA1*01:04-DPB1*06:01',
         'HLA-DPA1*01:04-DPB1*08:01', 'HLA-DPA1*01:04-DPB1*09:01', 'HLA-DPA1*01:04-DPB1*10:001', 'HLA-DPA1*01:04-DPB1*10:01',
         'HLA-DPA1*01:04-DPB1*10:101', 'HLA-DPA1*01:04-DPB1*10:201',
         'HLA-DPA1*01:04-DPB1*10:301', 'HLA-DPA1*01:04-DPB1*10:401', 'HLA-DPA1*01:04-DPB1*10:501', 'HLA-DPA1*01:04-DPB1*10:601',
         'HLA-DPA1*01:04-DPB1*10:701', 'HLA-DPA1*01:04-DPB1*10:801',
         'HLA-DPA1*01:04-DPB1*10:901', 'HLA-DPA1*01:04-DPB1*11:001', 'HLA-DPA1*01:04-DPB1*11:01', 'HLA-DPA1*01:04-DPB1*11:101',
         'HLA-DPA1*01:04-DPB1*11:201', 'HLA-DPA1*01:04-DPB1*11:301',
         'HLA-DPA1*01:04-DPB1*11:401', 'HLA-DPA1*01:04-DPB1*11:501', 'HLA-DPA1*01:04-DPB1*11:601', 'HLA-DPA1*01:04-DPB1*11:701',
         'HLA-DPA1*01:04-DPB1*11:801', 'HLA-DPA1*01:04-DPB1*11:901',
         'HLA-DPA1*01:04-DPB1*12:101', 'HLA-DPA1*01:04-DPB1*12:201', 'HLA-DPA1*01:04-DPB1*12:301', 'HLA-DPA1*01:04-DPB1*12:401',
         'HLA-DPA1*01:04-DPB1*12:501', 'HLA-DPA1*01:04-DPB1*12:601',
         'HLA-DPA1*01:04-DPB1*12:701', 'HLA-DPA1*01:04-DPB1*12:801', 'HLA-DPA1*01:04-DPB1*12:901', 'HLA-DPA1*01:04-DPB1*13:001',
         'HLA-DPA1*01:04-DPB1*13:01', 'HLA-DPA1*01:04-DPB1*13:101',
         'HLA-DPA1*01:04-DPB1*13:201', 'HLA-DPA1*01:04-DPB1*13:301', 'HLA-DPA1*01:04-DPB1*13:401', 'HLA-DPA1*01:04-DPB1*14:01',
         'HLA-DPA1*01:04-DPB1*15:01', 'HLA-DPA1*01:04-DPB1*16:01',
         'HLA-DPA1*01:04-DPB1*17:01', 'HLA-DPA1*01:04-DPB1*18:01', 'HLA-DPA1*01:04-DPB1*19:01', 'HLA-DPA1*01:04-DPB1*20:01',
         'HLA-DPA1*01:04-DPB1*21:01', 'HLA-DPA1*01:04-DPB1*22:01',
         'HLA-DPA1*01:04-DPB1*23:01', 'HLA-DPA1*01:04-DPB1*24:01', 'HLA-DPA1*01:04-DPB1*25:01', 'HLA-DPA1*01:04-DPB1*26:01',
         'HLA-DPA1*01:04-DPB1*27:01', 'HLA-DPA1*01:04-DPB1*28:01',
         'HLA-DPA1*01:04-DPB1*29:01', 'HLA-DPA1*01:04-DPB1*30:01', 'HLA-DPA1*01:04-DPB1*31:01', 'HLA-DPA1*01:04-DPB1*32:01',
         'HLA-DPA1*01:04-DPB1*33:01', 'HLA-DPA1*01:04-DPB1*34:01',
         'HLA-DPA1*01:04-DPB1*35:01', 'HLA-DPA1*01:04-DPB1*36:01', 'HLA-DPA1*01:04-DPB1*37:01', 'HLA-DPA1*01:04-DPB1*38:01',
         'HLA-DPA1*01:04-DPB1*39:01', 'HLA-DPA1*01:04-DPB1*40:01',
         'HLA-DPA1*01:04-DPB1*41:01', 'HLA-DPA1*01:04-DPB1*44:01', 'HLA-DPA1*01:04-DPB1*45:01', 'HLA-DPA1*01:04-DPB1*46:01',
         'HLA-DPA1*01:04-DPB1*47:01', 'HLA-DPA1*01:04-DPB1*48:01',
         'HLA-DPA1*01:04-DPB1*49:01', 'HLA-DPA1*01:04-DPB1*50:01', 'HLA-DPA1*01:04-DPB1*51:01', 'HLA-DPA1*01:04-DPB1*52:01',
         'HLA-DPA1*01:04-DPB1*53:01', 'HLA-DPA1*01:04-DPB1*54:01',
         'HLA-DPA1*01:04-DPB1*55:01', 'HLA-DPA1*01:04-DPB1*56:01', 'HLA-DPA1*01:04-DPB1*58:01', 'HLA-DPA1*01:04-DPB1*59:01',
         'HLA-DPA1*01:04-DPB1*60:01', 'HLA-DPA1*01:04-DPB1*62:01',
         'HLA-DPA1*01:04-DPB1*63:01', 'HLA-DPA1*01:04-DPB1*65:01', 'HLA-DPA1*01:04-DPB1*66:01', 'HLA-DPA1*01:04-DPB1*67:01',
         'HLA-DPA1*01:04-DPB1*68:01', 'HLA-DPA1*01:04-DPB1*69:01',
         'HLA-DPA1*01:04-DPB1*70:01', 'HLA-DPA1*01:04-DPB1*71:01', 'HLA-DPA1*01:04-DPB1*72:01', 'HLA-DPA1*01:04-DPB1*73:01',
         'HLA-DPA1*01:04-DPB1*74:01', 'HLA-DPA1*01:04-DPB1*75:01',
         'HLA-DPA1*01:04-DPB1*76:01', 'HLA-DPA1*01:04-DPB1*77:01', 'HLA-DPA1*01:04-DPB1*78:01', 'HLA-DPA1*01:04-DPB1*79:01',
         'HLA-DPA1*01:04-DPB1*80:01', 'HLA-DPA1*01:04-DPB1*81:01',
         'HLA-DPA1*01:04-DPB1*82:01', 'HLA-DPA1*01:04-DPB1*83:01', 'HLA-DPA1*01:04-DPB1*84:01', 'HLA-DPA1*01:04-DPB1*85:01',
         'HLA-DPA1*01:04-DPB1*86:01', 'HLA-DPA1*01:04-DPB1*87:01',
         'HLA-DPA1*01:04-DPB1*88:01', 'HLA-DPA1*01:04-DPB1*89:01', 'HLA-DPA1*01:04-DPB1*90:01', 'HLA-DPA1*01:04-DPB1*91:01',
         'HLA-DPA1*01:04-DPB1*92:01', 'HLA-DPA1*01:04-DPB1*93:01',
         'HLA-DPA1*01:04-DPB1*94:01', 'HLA-DPA1*01:04-DPB1*95:01', 'HLA-DPA1*01:04-DPB1*96:01', 'HLA-DPA1*01:04-DPB1*97:01',
         'HLA-DPA1*01:04-DPB1*98:01', 'HLA-DPA1*01:04-DPB1*99:01',
         'HLA-DPA1*01:05-DPB1*01:01', 'HLA-DPA1*01:05-DPB1*02:01', 'HLA-DPA1*01:05-DPB1*02:02', 'HLA-DPA1*01:05-DPB1*03:01',
         'HLA-DPA1*01:05-DPB1*04:01', 'HLA-DPA1*01:05-DPB1*04:02',
         'HLA-DPA1*01:05-DPB1*05:01', 'HLA-DPA1*01:05-DPB1*06:01', 'HLA-DPA1*01:05-DPB1*08:01', 'HLA-DPA1*01:05-DPB1*09:01',
         'HLA-DPA1*01:05-DPB1*10:001', 'HLA-DPA1*01:05-DPB1*10:01',
         'HLA-DPA1*01:05-DPB1*10:101', 'HLA-DPA1*01:05-DPB1*10:201', 'HLA-DPA1*01:05-DPB1*10:301', 'HLA-DPA1*01:05-DPB1*10:401',
         'HLA-DPA1*01:05-DPB1*10:501', 'HLA-DPA1*01:05-DPB1*10:601',
         'HLA-DPA1*01:05-DPB1*10:701', 'HLA-DPA1*01:05-DPB1*10:801', 'HLA-DPA1*01:05-DPB1*10:901', 'HLA-DPA1*01:05-DPB1*11:001',
         'HLA-DPA1*01:05-DPB1*11:01', 'HLA-DPA1*01:05-DPB1*11:101',
         'HLA-DPA1*01:05-DPB1*11:201', 'HLA-DPA1*01:05-DPB1*11:301', 'HLA-DPA1*01:05-DPB1*11:401', 'HLA-DPA1*01:05-DPB1*11:501',
         'HLA-DPA1*01:05-DPB1*11:601', 'HLA-DPA1*01:05-DPB1*11:701',
         'HLA-DPA1*01:05-DPB1*11:801', 'HLA-DPA1*01:05-DPB1*11:901', 'HLA-DPA1*01:05-DPB1*12:101', 'HLA-DPA1*01:05-DPB1*12:201',
         'HLA-DPA1*01:05-DPB1*12:301', 'HLA-DPA1*01:05-DPB1*12:401',
         'HLA-DPA1*01:05-DPB1*12:501', 'HLA-DPA1*01:05-DPB1*12:601', 'HLA-DPA1*01:05-DPB1*12:701', 'HLA-DPA1*01:05-DPB1*12:801',
         'HLA-DPA1*01:05-DPB1*12:901', 'HLA-DPA1*01:05-DPB1*13:001',
         'HLA-DPA1*01:05-DPB1*13:01', 'HLA-DPA1*01:05-DPB1*13:101', 'HLA-DPA1*01:05-DPB1*13:201', 'HLA-DPA1*01:05-DPB1*13:301',
         'HLA-DPA1*01:05-DPB1*13:401', 'HLA-DPA1*01:05-DPB1*14:01',
         'HLA-DPA1*01:05-DPB1*15:01', 'HLA-DPA1*01:05-DPB1*16:01', 'HLA-DPA1*01:05-DPB1*17:01', 'HLA-DPA1*01:05-DPB1*18:01',
         'HLA-DPA1*01:05-DPB1*19:01', 'HLA-DPA1*01:05-DPB1*20:01',
         'HLA-DPA1*01:05-DPB1*21:01', 'HLA-DPA1*01:05-DPB1*22:01', 'HLA-DPA1*01:05-DPB1*23:01', 'HLA-DPA1*01:05-DPB1*24:01',
         'HLA-DPA1*01:05-DPB1*25:01', 'HLA-DPA1*01:05-DPB1*26:01',
         'HLA-DPA1*01:05-DPB1*27:01', 'HLA-DPA1*01:05-DPB1*28:01', 'HLA-DPA1*01:05-DPB1*29:01', 'HLA-DPA1*01:05-DPB1*30:01',
         'HLA-DPA1*01:05-DPB1*31:01', 'HLA-DPA1*01:05-DPB1*32:01',
         'HLA-DPA1*01:05-DPB1*33:01', 'HLA-DPA1*01:05-DPB1*34:01', 'HLA-DPA1*01:05-DPB1*35:01', 'HLA-DPA1*01:05-DPB1*36:01',
         'HLA-DPA1*01:05-DPB1*37:01', 'HLA-DPA1*01:05-DPB1*38:01',
         'HLA-DPA1*01:05-DPB1*39:01', 'HLA-DPA1*01:05-DPB1*40:01', 'HLA-DPA1*01:05-DPB1*41:01', 'HLA-DPA1*01:05-DPB1*44:01',
         'HLA-DPA1*01:05-DPB1*45:01', 'HLA-DPA1*01:05-DPB1*46:01',
         'HLA-DPA1*01:05-DPB1*47:01', 'HLA-DPA1*01:05-DPB1*48:01', 'HLA-DPA1*01:05-DPB1*49:01', 'HLA-DPA1*01:05-DPB1*50:01',
         'HLA-DPA1*01:05-DPB1*51:01', 'HLA-DPA1*01:05-DPB1*52:01',
         'HLA-DPA1*01:05-DPB1*53:01', 'HLA-DPA1*01:05-DPB1*54:01', 'HLA-DPA1*01:05-DPB1*55:01', 'HLA-DPA1*01:05-DPB1*56:01',
         'HLA-DPA1*01:05-DPB1*58:01', 'HLA-DPA1*01:05-DPB1*59:01',
         'HLA-DPA1*01:05-DPB1*60:01', 'HLA-DPA1*01:05-DPB1*62:01', 'HLA-DPA1*01:05-DPB1*63:01', 'HLA-DPA1*01:05-DPB1*65:01',
         'HLA-DPA1*01:05-DPB1*66:01', 'HLA-DPA1*01:05-DPB1*67:01',
         'HLA-DPA1*01:05-DPB1*68:01', 'HLA-DPA1*01:05-DPB1*69:01', 'HLA-DPA1*01:05-DPB1*70:01', 'HLA-DPA1*01:05-DPB1*71:01',
         'HLA-DPA1*01:05-DPB1*72:01', 'HLA-DPA1*01:05-DPB1*73:01',
         'HLA-DPA1*01:05-DPB1*74:01', 'HLA-DPA1*01:05-DPB1*75:01', 'HLA-DPA1*01:05-DPB1*76:01', 'HLA-DPA1*01:05-DPB1*77:01',
         'HLA-DPA1*01:05-DPB1*78:01', 'HLA-DPA1*01:05-DPB1*79:01',
         'HLA-DPA1*01:05-DPB1*80:01', 'HLA-DPA1*01:05-DPB1*81:01', 'HLA-DPA1*01:05-DPB1*82:01', 'HLA-DPA1*01:05-DPB1*83:01',
         'HLA-DPA1*01:05-DPB1*84:01', 'HLA-DPA1*01:05-DPB1*85:01',
         'HLA-DPA1*01:05-DPB1*86:01', 'HLA-DPA1*01:05-DPB1*87:01', 'HLA-DPA1*01:05-DPB1*88:01', 'HLA-DPA1*01:05-DPB1*89:01',
         'HLA-DPA1*01:05-DPB1*90:01', 'HLA-DPA1*01:05-DPB1*91:01',
         'HLA-DPA1*01:05-DPB1*92:01', 'HLA-DPA1*01:05-DPB1*93:01', 'HLA-DPA1*01:05-DPB1*94:01', 'HLA-DPA1*01:05-DPB1*95:01',
         'HLA-DPA1*01:05-DPB1*96:01', 'HLA-DPA1*01:05-DPB1*97:01',
         'HLA-DPA1*01:05-DPB1*98:01', 'HLA-DPA1*01:05-DPB1*99:01', 'HLA-DPA1*01:06-DPB1*01:01', 'HLA-DPA1*01:06-DPB1*02:01',
         'HLA-DPA1*01:06-DPB1*02:02', 'HLA-DPA1*01:06-DPB1*03:01',
         'HLA-DPA1*01:06-DPB1*04:01', 'HLA-DPA1*01:06-DPB1*04:02', 'HLA-DPA1*01:06-DPB1*05:01', 'HLA-DPA1*01:06-DPB1*06:01',
         'HLA-DPA1*01:06-DPB1*08:01', 'HLA-DPA1*01:06-DPB1*09:01',
         'HLA-DPA1*01:06-DPB1*10:001', 'HLA-DPA1*01:06-DPB1*10:01', 'HLA-DPA1*01:06-DPB1*10:101', 'HLA-DPA1*01:06-DPB1*10:201',
         'HLA-DPA1*01:06-DPB1*10:301', 'HLA-DPA1*01:06-DPB1*10:401',
         'HLA-DPA1*01:06-DPB1*10:501', 'HLA-DPA1*01:06-DPB1*10:601', 'HLA-DPA1*01:06-DPB1*10:701', 'HLA-DPA1*01:06-DPB1*10:801',
         'HLA-DPA1*01:06-DPB1*10:901', 'HLA-DPA1*01:06-DPB1*11:001',
         'HLA-DPA1*01:06-DPB1*11:01', 'HLA-DPA1*01:06-DPB1*11:101', 'HLA-DPA1*01:06-DPB1*11:201', 'HLA-DPA1*01:06-DPB1*11:301',
         'HLA-DPA1*01:06-DPB1*11:401', 'HLA-DPA1*01:06-DPB1*11:501',
         'HLA-DPA1*01:06-DPB1*11:601', 'HLA-DPA1*01:06-DPB1*11:701', 'HLA-DPA1*01:06-DPB1*11:801', 'HLA-DPA1*01:06-DPB1*11:901',
         'HLA-DPA1*01:06-DPB1*12:101', 'HLA-DPA1*01:06-DPB1*12:201',
         'HLA-DPA1*01:06-DPB1*12:301', 'HLA-DPA1*01:06-DPB1*12:401', 'HLA-DPA1*01:06-DPB1*12:501', 'HLA-DPA1*01:06-DPB1*12:601',
         'HLA-DPA1*01:06-DPB1*12:701', 'HLA-DPA1*01:06-DPB1*12:801',
         'HLA-DPA1*01:06-DPB1*12:901', 'HLA-DPA1*01:06-DPB1*13:001', 'HLA-DPA1*01:06-DPB1*13:01', 'HLA-DPA1*01:06-DPB1*13:101',
         'HLA-DPA1*01:06-DPB1*13:201', 'HLA-DPA1*01:06-DPB1*13:301',
         'HLA-DPA1*01:06-DPB1*13:401', 'HLA-DPA1*01:06-DPB1*14:01', 'HLA-DPA1*01:06-DPB1*15:01', 'HLA-DPA1*01:06-DPB1*16:01',
         'HLA-DPA1*01:06-DPB1*17:01', 'HLA-DPA1*01:06-DPB1*18:01',
         'HLA-DPA1*01:06-DPB1*19:01', 'HLA-DPA1*01:06-DPB1*20:01', 'HLA-DPA1*01:06-DPB1*21:01', 'HLA-DPA1*01:06-DPB1*22:01',
         'HLA-DPA1*01:06-DPB1*23:01', 'HLA-DPA1*01:06-DPB1*24:01',
         'HLA-DPA1*01:06-DPB1*25:01', 'HLA-DPA1*01:06-DPB1*26:01', 'HLA-DPA1*01:06-DPB1*27:01', 'HLA-DPA1*01:06-DPB1*28:01',
         'HLA-DPA1*01:06-DPB1*29:01', 'HLA-DPA1*01:06-DPB1*30:01',
         'HLA-DPA1*01:06-DPB1*31:01', 'HLA-DPA1*01:06-DPB1*32:01', 'HLA-DPA1*01:06-DPB1*33:01', 'HLA-DPA1*01:06-DPB1*34:01',
         'HLA-DPA1*01:06-DPB1*35:01', 'HLA-DPA1*01:06-DPB1*36:01',
         'HLA-DPA1*01:06-DPB1*37:01', 'HLA-DPA1*01:06-DPB1*38:01', 'HLA-DPA1*01:06-DPB1*39:01', 'HLA-DPA1*01:06-DPB1*40:01',
         'HLA-DPA1*01:06-DPB1*41:01', 'HLA-DPA1*01:06-DPB1*44:01',
         'HLA-DPA1*01:06-DPB1*45:01', 'HLA-DPA1*01:06-DPB1*46:01', 'HLA-DPA1*01:06-DPB1*47:01', 'HLA-DPA1*01:06-DPB1*48:01',
         'HLA-DPA1*01:06-DPB1*49:01', 'HLA-DPA1*01:06-DPB1*50:01',
         'HLA-DPA1*01:06-DPB1*51:01', 'HLA-DPA1*01:06-DPB1*52:01', 'HLA-DPA1*01:06-DPB1*53:01', 'HLA-DPA1*01:06-DPB1*54:01',
         'HLA-DPA1*01:06-DPB1*55:01', 'HLA-DPA1*01:06-DPB1*56:01',
         'HLA-DPA1*01:06-DPB1*58:01', 'HLA-DPA1*01:06-DPB1*59:01', 'HLA-DPA1*01:06-DPB1*60:01', 'HLA-DPA1*01:06-DPB1*62:01',
         'HLA-DPA1*01:06-DPB1*63:01', 'HLA-DPA1*01:06-DPB1*65:01',
         'HLA-DPA1*01:06-DPB1*66:01', 'HLA-DPA1*01:06-DPB1*67:01', 'HLA-DPA1*01:06-DPB1*68:01', 'HLA-DPA1*01:06-DPB1*69:01',
         'HLA-DPA1*01:06-DPB1*70:01', 'HLA-DPA1*01:06-DPB1*71:01',
         'HLA-DPA1*01:06-DPB1*72:01', 'HLA-DPA1*01:06-DPB1*73:01', 'HLA-DPA1*01:06-DPB1*74:01', 'HLA-DPA1*01:06-DPB1*75:01',
         'HLA-DPA1*01:06-DPB1*76:01', 'HLA-DPA1*01:06-DPB1*77:01',
         'HLA-DPA1*01:06-DPB1*78:01', 'HLA-DPA1*01:06-DPB1*79:01', 'HLA-DPA1*01:06-DPB1*80:01', 'HLA-DPA1*01:06-DPB1*81:01',
         'HLA-DPA1*01:06-DPB1*82:01', 'HLA-DPA1*01:06-DPB1*83:01',
         'HLA-DPA1*01:06-DPB1*84:01', 'HLA-DPA1*01:06-DPB1*85:01', 'HLA-DPA1*01:06-DPB1*86:01', 'HLA-DPA1*01:06-DPB1*87:01',
         'HLA-DPA1*01:06-DPB1*88:01', 'HLA-DPA1*01:06-DPB1*89:01',
         'HLA-DPA1*01:06-DPB1*90:01', 'HLA-DPA1*01:06-DPB1*91:01', 'HLA-DPA1*01:06-DPB1*92:01', 'HLA-DPA1*01:06-DPB1*93:01',
         'HLA-DPA1*01:06-DPB1*94:01', 'HLA-DPA1*01:06-DPB1*95:01',
         'HLA-DPA1*01:06-DPB1*96:01', 'HLA-DPA1*01:06-DPB1*97:01', 'HLA-DPA1*01:06-DPB1*98:01', 'HLA-DPA1*01:06-DPB1*99:01',
         'HLA-DPA1*01:07-DPB1*01:01', 'HLA-DPA1*01:07-DPB1*02:01',
         'HLA-DPA1*01:07-DPB1*02:02', 'HLA-DPA1*01:07-DPB1*03:01', 'HLA-DPA1*01:07-DPB1*04:01', 'HLA-DPA1*01:07-DPB1*04:02',
         'HLA-DPA1*01:07-DPB1*05:01', 'HLA-DPA1*01:07-DPB1*06:01',
         'HLA-DPA1*01:07-DPB1*08:01', 'HLA-DPA1*01:07-DPB1*09:01', 'HLA-DPA1*01:07-DPB1*10:001', 'HLA-DPA1*01:07-DPB1*10:01',
         'HLA-DPA1*01:07-DPB1*10:101', 'HLA-DPA1*01:07-DPB1*10:201',
         'HLA-DPA1*01:07-DPB1*10:301', 'HLA-DPA1*01:07-DPB1*10:401', 'HLA-DPA1*01:07-DPB1*10:501', 'HLA-DPA1*01:07-DPB1*10:601',
         'HLA-DPA1*01:07-DPB1*10:701', 'HLA-DPA1*01:07-DPB1*10:801',
         'HLA-DPA1*01:07-DPB1*10:901', 'HLA-DPA1*01:07-DPB1*11:001', 'HLA-DPA1*01:07-DPB1*11:01', 'HLA-DPA1*01:07-DPB1*11:101',
         'HLA-DPA1*01:07-DPB1*11:201', 'HLA-DPA1*01:07-DPB1*11:301',
         'HLA-DPA1*01:07-DPB1*11:401', 'HLA-DPA1*01:07-DPB1*11:501', 'HLA-DPA1*01:07-DPB1*11:601', 'HLA-DPA1*01:07-DPB1*11:701',
         'HLA-DPA1*01:07-DPB1*11:801', 'HLA-DPA1*01:07-DPB1*11:901',
         'HLA-DPA1*01:07-DPB1*12:101', 'HLA-DPA1*01:07-DPB1*12:201', 'HLA-DPA1*01:07-DPB1*12:301', 'HLA-DPA1*01:07-DPB1*12:401',
         'HLA-DPA1*01:07-DPB1*12:501', 'HLA-DPA1*01:07-DPB1*12:601',
         'HLA-DPA1*01:07-DPB1*12:701', 'HLA-DPA1*01:07-DPB1*12:801', 'HLA-DPA1*01:07-DPB1*12:901', 'HLA-DPA1*01:07-DPB1*13:001',
         'HLA-DPA1*01:07-DPB1*13:01', 'HLA-DPA1*01:07-DPB1*13:101',
         'HLA-DPA1*01:07-DPB1*13:201', 'HLA-DPA1*01:07-DPB1*13:301', 'HLA-DPA1*01:07-DPB1*13:401', 'HLA-DPA1*01:07-DPB1*14:01',
         'HLA-DPA1*01:07-DPB1*15:01', 'HLA-DPA1*01:07-DPB1*16:01',
         'HLA-DPA1*01:07-DPB1*17:01', 'HLA-DPA1*01:07-DPB1*18:01', 'HLA-DPA1*01:07-DPB1*19:01', 'HLA-DPA1*01:07-DPB1*20:01',
         'HLA-DPA1*01:07-DPB1*21:01', 'HLA-DPA1*01:07-DPB1*22:01',
         'HLA-DPA1*01:07-DPB1*23:01', 'HLA-DPA1*01:07-DPB1*24:01', 'HLA-DPA1*01:07-DPB1*25:01', 'HLA-DPA1*01:07-DPB1*26:01',
         'HLA-DPA1*01:07-DPB1*27:01', 'HLA-DPA1*01:07-DPB1*28:01',
         'HLA-DPA1*01:07-DPB1*29:01', 'HLA-DPA1*01:07-DPB1*30:01', 'HLA-DPA1*01:07-DPB1*31:01', 'HLA-DPA1*01:07-DPB1*32:01',
         'HLA-DPA1*01:07-DPB1*33:01', 'HLA-DPA1*01:07-DPB1*34:01',
         'HLA-DPA1*01:07-DPB1*35:01', 'HLA-DPA1*01:07-DPB1*36:01', 'HLA-DPA1*01:07-DPB1*37:01', 'HLA-DPA1*01:07-DPB1*38:01',
         'HLA-DPA1*01:07-DPB1*39:01', 'HLA-DPA1*01:07-DPB1*40:01',
         'HLA-DPA1*01:07-DPB1*41:01', 'HLA-DPA1*01:07-DPB1*44:01', 'HLA-DPA1*01:07-DPB1*45:01', 'HLA-DPA1*01:07-DPB1*46:01',
         'HLA-DPA1*01:07-DPB1*47:01', 'HLA-DPA1*01:07-DPB1*48:01',
         'HLA-DPA1*01:07-DPB1*49:01', 'HLA-DPA1*01:07-DPB1*50:01', 'HLA-DPA1*01:07-DPB1*51:01', 'HLA-DPA1*01:07-DPB1*52:01',
         'HLA-DPA1*01:07-DPB1*53:01', 'HLA-DPA1*01:07-DPB1*54:01',
         'HLA-DPA1*01:07-DPB1*55:01', 'HLA-DPA1*01:07-DPB1*56:01', 'HLA-DPA1*01:07-DPB1*58:01', 'HLA-DPA1*01:07-DPB1*59:01',
         'HLA-DPA1*01:07-DPB1*60:01', 'HLA-DPA1*01:07-DPB1*62:01',
         'HLA-DPA1*01:07-DPB1*63:01', 'HLA-DPA1*01:07-DPB1*65:01', 'HLA-DPA1*01:07-DPB1*66:01', 'HLA-DPA1*01:07-DPB1*67:01',
         'HLA-DPA1*01:07-DPB1*68:01', 'HLA-DPA1*01:07-DPB1*69:01',
         'HLA-DPA1*01:07-DPB1*70:01', 'HLA-DPA1*01:07-DPB1*71:01', 'HLA-DPA1*01:07-DPB1*72:01', 'HLA-DPA1*01:07-DPB1*73:01',
         'HLA-DPA1*01:07-DPB1*74:01', 'HLA-DPA1*01:07-DPB1*75:01',
         'HLA-DPA1*01:07-DPB1*76:01', 'HLA-DPA1*01:07-DPB1*77:01', 'HLA-DPA1*01:07-DPB1*78:01', 'HLA-DPA1*01:07-DPB1*79:01',
         'HLA-DPA1*01:07-DPB1*80:01', 'HLA-DPA1*01:07-DPB1*81:01',
         'HLA-DPA1*01:07-DPB1*82:01', 'HLA-DPA1*01:07-DPB1*83:01', 'HLA-DPA1*01:07-DPB1*84:01', 'HLA-DPA1*01:07-DPB1*85:01',
         'HLA-DPA1*01:07-DPB1*86:01', 'HLA-DPA1*01:07-DPB1*87:01',
         'HLA-DPA1*01:07-DPB1*88:01', 'HLA-DPA1*01:07-DPB1*89:01', 'HLA-DPA1*01:07-DPB1*90:01', 'HLA-DPA1*01:07-DPB1*91:01',
         'HLA-DPA1*01:07-DPB1*92:01', 'HLA-DPA1*01:07-DPB1*93:01',
         'HLA-DPA1*01:07-DPB1*94:01', 'HLA-DPA1*01:07-DPB1*95:01', 'HLA-DPA1*01:07-DPB1*96:01', 'HLA-DPA1*01:07-DPB1*97:01',
         'HLA-DPA1*01:07-DPB1*98:01', 'HLA-DPA1*01:07-DPB1*99:01',
         'HLA-DPA1*01:08-DPB1*01:01', 'HLA-DPA1*01:08-DPB1*02:01', 'HLA-DPA1*01:08-DPB1*02:02', 'HLA-DPA1*01:08-DPB1*03:01',
         'HLA-DPA1*01:08-DPB1*04:01', 'HLA-DPA1*01:08-DPB1*04:02',
         'HLA-DPA1*01:08-DPB1*05:01', 'HLA-DPA1*01:08-DPB1*06:01', 'HLA-DPA1*01:08-DPB1*08:01', 'HLA-DPA1*01:08-DPB1*09:01',
         'HLA-DPA1*01:08-DPB1*10:001', 'HLA-DPA1*01:08-DPB1*10:01',
         'HLA-DPA1*01:08-DPB1*10:101', 'HLA-DPA1*01:08-DPB1*10:201', 'HLA-DPA1*01:08-DPB1*10:301', 'HLA-DPA1*01:08-DPB1*10:401',
         'HLA-DPA1*01:08-DPB1*10:501', 'HLA-DPA1*01:08-DPB1*10:601',
         'HLA-DPA1*01:08-DPB1*10:701', 'HLA-DPA1*01:08-DPB1*10:801', 'HLA-DPA1*01:08-DPB1*10:901', 'HLA-DPA1*01:08-DPB1*11:001',
         'HLA-DPA1*01:08-DPB1*11:01', 'HLA-DPA1*01:08-DPB1*11:101',
         'HLA-DPA1*01:08-DPB1*11:201', 'HLA-DPA1*01:08-DPB1*11:301', 'HLA-DPA1*01:08-DPB1*11:401', 'HLA-DPA1*01:08-DPB1*11:501',
         'HLA-DPA1*01:08-DPB1*11:601', 'HLA-DPA1*01:08-DPB1*11:701',
         'HLA-DPA1*01:08-DPB1*11:801', 'HLA-DPA1*01:08-DPB1*11:901', 'HLA-DPA1*01:08-DPB1*12:101', 'HLA-DPA1*01:08-DPB1*12:201',
         'HLA-DPA1*01:08-DPB1*12:301', 'HLA-DPA1*01:08-DPB1*12:401',
         'HLA-DPA1*01:08-DPB1*12:501', 'HLA-DPA1*01:08-DPB1*12:601', 'HLA-DPA1*01:08-DPB1*12:701', 'HLA-DPA1*01:08-DPB1*12:801',
         'HLA-DPA1*01:08-DPB1*12:901', 'HLA-DPA1*01:08-DPB1*13:001',
         'HLA-DPA1*01:08-DPB1*13:01', 'HLA-DPA1*01:08-DPB1*13:101', 'HLA-DPA1*01:08-DPB1*13:201', 'HLA-DPA1*01:08-DPB1*13:301',
         'HLA-DPA1*01:08-DPB1*13:401', 'HLA-DPA1*01:08-DPB1*14:01',
         'HLA-DPA1*01:08-DPB1*15:01', 'HLA-DPA1*01:08-DPB1*16:01', 'HLA-DPA1*01:08-DPB1*17:01', 'HLA-DPA1*01:08-DPB1*18:01',
         'HLA-DPA1*01:08-DPB1*19:01', 'HLA-DPA1*01:08-DPB1*20:01',
         'HLA-DPA1*01:08-DPB1*21:01', 'HLA-DPA1*01:08-DPB1*22:01', 'HLA-DPA1*01:08-DPB1*23:01', 'HLA-DPA1*01:08-DPB1*24:01',
         'HLA-DPA1*01:08-DPB1*25:01', 'HLA-DPA1*01:08-DPB1*26:01',
         'HLA-DPA1*01:08-DPB1*27:01', 'HLA-DPA1*01:08-DPB1*28:01', 'HLA-DPA1*01:08-DPB1*29:01', 'HLA-DPA1*01:08-DPB1*30:01',
         'HLA-DPA1*01:08-DPB1*31:01', 'HLA-DPA1*01:08-DPB1*32:01',
         'HLA-DPA1*01:08-DPB1*33:01', 'HLA-DPA1*01:08-DPB1*34:01', 'HLA-DPA1*01:08-DPB1*35:01', 'HLA-DPA1*01:08-DPB1*36:01',
         'HLA-DPA1*01:08-DPB1*37:01', 'HLA-DPA1*01:08-DPB1*38:01',
         'HLA-DPA1*01:08-DPB1*39:01', 'HLA-DPA1*01:08-DPB1*40:01', 'HLA-DPA1*01:08-DPB1*41:01', 'HLA-DPA1*01:08-DPB1*44:01',
         'HLA-DPA1*01:08-DPB1*45:01', 'HLA-DPA1*01:08-DPB1*46:01',
         'HLA-DPA1*01:08-DPB1*47:01', 'HLA-DPA1*01:08-DPB1*48:01', 'HLA-DPA1*01:08-DPB1*49:01', 'HLA-DPA1*01:08-DPB1*50:01',
         'HLA-DPA1*01:08-DPB1*51:01', 'HLA-DPA1*01:08-DPB1*52:01',
         'HLA-DPA1*01:08-DPB1*53:01', 'HLA-DPA1*01:08-DPB1*54:01', 'HLA-DPA1*01:08-DPB1*55:01', 'HLA-DPA1*01:08-DPB1*56:01',
         'HLA-DPA1*01:08-DPB1*58:01', 'HLA-DPA1*01:08-DPB1*59:01',
         'HLA-DPA1*01:08-DPB1*60:01', 'HLA-DPA1*01:08-DPB1*62:01', 'HLA-DPA1*01:08-DPB1*63:01', 'HLA-DPA1*01:08-DPB1*65:01',
         'HLA-DPA1*01:08-DPB1*66:01', 'HLA-DPA1*01:08-DPB1*67:01',
         'HLA-DPA1*01:08-DPB1*68:01', 'HLA-DPA1*01:08-DPB1*69:01', 'HLA-DPA1*01:08-DPB1*70:01', 'HLA-DPA1*01:08-DPB1*71:01',
         'HLA-DPA1*01:08-DPB1*72:01', 'HLA-DPA1*01:08-DPB1*73:01',
         'HLA-DPA1*01:08-DPB1*74:01', 'HLA-DPA1*01:08-DPB1*75:01', 'HLA-DPA1*01:08-DPB1*76:01', 'HLA-DPA1*01:08-DPB1*77:01',
         'HLA-DPA1*01:08-DPB1*78:01', 'HLA-DPA1*01:08-DPB1*79:01',
         'HLA-DPA1*01:08-DPB1*80:01', 'HLA-DPA1*01:08-DPB1*81:01', 'HLA-DPA1*01:08-DPB1*82:01', 'HLA-DPA1*01:08-DPB1*83:01',
         'HLA-DPA1*01:08-DPB1*84:01', 'HLA-DPA1*01:08-DPB1*85:01',
         'HLA-DPA1*01:08-DPB1*86:01', 'HLA-DPA1*01:08-DPB1*87:01', 'HLA-DPA1*01:08-DPB1*88:01', 'HLA-DPA1*01:08-DPB1*89:01',
         'HLA-DPA1*01:08-DPB1*90:01', 'HLA-DPA1*01:08-DPB1*91:01',
         'HLA-DPA1*01:08-DPB1*92:01', 'HLA-DPA1*01:08-DPB1*93:01', 'HLA-DPA1*01:08-DPB1*94:01', 'HLA-DPA1*01:08-DPB1*95:01',
         'HLA-DPA1*01:08-DPB1*96:01', 'HLA-DPA1*01:08-DPB1*97:01',
         'HLA-DPA1*01:08-DPB1*98:01', 'HLA-DPA1*01:08-DPB1*99:01', 'HLA-DPA1*01:09-DPB1*01:01', 'HLA-DPA1*01:09-DPB1*02:01',
         'HLA-DPA1*01:09-DPB1*02:02', 'HLA-DPA1*01:09-DPB1*03:01',
         'HLA-DPA1*01:09-DPB1*04:01', 'HLA-DPA1*01:09-DPB1*04:02', 'HLA-DPA1*01:09-DPB1*05:01', 'HLA-DPA1*01:09-DPB1*06:01',
         'HLA-DPA1*01:09-DPB1*08:01', 'HLA-DPA1*01:09-DPB1*09:01',
         'HLA-DPA1*01:09-DPB1*10:001', 'HLA-DPA1*01:09-DPB1*10:01', 'HLA-DPA1*01:09-DPB1*10:101', 'HLA-DPA1*01:09-DPB1*10:201',
         'HLA-DPA1*01:09-DPB1*10:301', 'HLA-DPA1*01:09-DPB1*10:401',
         'HLA-DPA1*01:09-DPB1*10:501', 'HLA-DPA1*01:09-DPB1*10:601', 'HLA-DPA1*01:09-DPB1*10:701', 'HLA-DPA1*01:09-DPB1*10:801',
         'HLA-DPA1*01:09-DPB1*10:901', 'HLA-DPA1*01:09-DPB1*11:001',
         'HLA-DPA1*01:09-DPB1*11:01', 'HLA-DPA1*01:09-DPB1*11:101', 'HLA-DPA1*01:09-DPB1*11:201', 'HLA-DPA1*01:09-DPB1*11:301',
         'HLA-DPA1*01:09-DPB1*11:401', 'HLA-DPA1*01:09-DPB1*11:501',
         'HLA-DPA1*01:09-DPB1*11:601', 'HLA-DPA1*01:09-DPB1*11:701', 'HLA-DPA1*01:09-DPB1*11:801', 'HLA-DPA1*01:09-DPB1*11:901',
         'HLA-DPA1*01:09-DPB1*12:101', 'HLA-DPA1*01:09-DPB1*12:201',
         'HLA-DPA1*01:09-DPB1*12:301', 'HLA-DPA1*01:09-DPB1*12:401', 'HLA-DPA1*01:09-DPB1*12:501', 'HLA-DPA1*01:09-DPB1*12:601',
         'HLA-DPA1*01:09-DPB1*12:701', 'HLA-DPA1*01:09-DPB1*12:801',
         'HLA-DPA1*01:09-DPB1*12:901', 'HLA-DPA1*01:09-DPB1*13:001', 'HLA-DPA1*01:09-DPB1*13:01', 'HLA-DPA1*01:09-DPB1*13:101',
         'HLA-DPA1*01:09-DPB1*13:201', 'HLA-DPA1*01:09-DPB1*13:301',
         'HLA-DPA1*01:09-DPB1*13:401', 'HLA-DPA1*01:09-DPB1*14:01', 'HLA-DPA1*01:09-DPB1*15:01', 'HLA-DPA1*01:09-DPB1*16:01',
         'HLA-DPA1*01:09-DPB1*17:01', 'HLA-DPA1*01:09-DPB1*18:01',
         'HLA-DPA1*01:09-DPB1*19:01', 'HLA-DPA1*01:09-DPB1*20:01', 'HLA-DPA1*01:09-DPB1*21:01', 'HLA-DPA1*01:09-DPB1*22:01',
         'HLA-DPA1*01:09-DPB1*23:01', 'HLA-DPA1*01:09-DPB1*24:01',
         'HLA-DPA1*01:09-DPB1*25:01', 'HLA-DPA1*01:09-DPB1*26:01', 'HLA-DPA1*01:09-DPB1*27:01', 'HLA-DPA1*01:09-DPB1*28:01',
         'HLA-DPA1*01:09-DPB1*29:01', 'HLA-DPA1*01:09-DPB1*30:01',
         'HLA-DPA1*01:09-DPB1*31:01', 'HLA-DPA1*01:09-DPB1*32:01', 'HLA-DPA1*01:09-DPB1*33:01', 'HLA-DPA1*01:09-DPB1*34:01',
         'HLA-DPA1*01:09-DPB1*35:01', 'HLA-DPA1*01:09-DPB1*36:01',
         'HLA-DPA1*01:09-DPB1*37:01', 'HLA-DPA1*01:09-DPB1*38:01', 'HLA-DPA1*01:09-DPB1*39:01', 'HLA-DPA1*01:09-DPB1*40:01',
         'HLA-DPA1*01:09-DPB1*41:01', 'HLA-DPA1*01:09-DPB1*44:01',
         'HLA-DPA1*01:09-DPB1*45:01', 'HLA-DPA1*01:09-DPB1*46:01', 'HLA-DPA1*01:09-DPB1*47:01', 'HLA-DPA1*01:09-DPB1*48:01',
         'HLA-DPA1*01:09-DPB1*49:01', 'HLA-DPA1*01:09-DPB1*50:01',
         'HLA-DPA1*01:09-DPB1*51:01', 'HLA-DPA1*01:09-DPB1*52:01', 'HLA-DPA1*01:09-DPB1*53:01', 'HLA-DPA1*01:09-DPB1*54:01',
         'HLA-DPA1*01:09-DPB1*55:01', 'HLA-DPA1*01:09-DPB1*56:01',
         'HLA-DPA1*01:09-DPB1*58:01', 'HLA-DPA1*01:09-DPB1*59:01', 'HLA-DPA1*01:09-DPB1*60:01', 'HLA-DPA1*01:09-DPB1*62:01',
         'HLA-DPA1*01:09-DPB1*63:01', 'HLA-DPA1*01:09-DPB1*65:01',
         'HLA-DPA1*01:09-DPB1*66:01', 'HLA-DPA1*01:09-DPB1*67:01', 'HLA-DPA1*01:09-DPB1*68:01', 'HLA-DPA1*01:09-DPB1*69:01',
         'HLA-DPA1*01:09-DPB1*70:01', 'HLA-DPA1*01:09-DPB1*71:01',
         'HLA-DPA1*01:09-DPB1*72:01', 'HLA-DPA1*01:09-DPB1*73:01', 'HLA-DPA1*01:09-DPB1*74:01', 'HLA-DPA1*01:09-DPB1*75:01',
         'HLA-DPA1*01:09-DPB1*76:01', 'HLA-DPA1*01:09-DPB1*77:01',
         'HLA-DPA1*01:09-DPB1*78:01', 'HLA-DPA1*01:09-DPB1*79:01', 'HLA-DPA1*01:09-DPB1*80:01', 'HLA-DPA1*01:09-DPB1*81:01',
         'HLA-DPA1*01:09-DPB1*82:01', 'HLA-DPA1*01:09-DPB1*83:01',
         'HLA-DPA1*01:09-DPB1*84:01', 'HLA-DPA1*01:09-DPB1*85:01', 'HLA-DPA1*01:09-DPB1*86:01', 'HLA-DPA1*01:09-DPB1*87:01',
         'HLA-DPA1*01:09-DPB1*88:01', 'HLA-DPA1*01:09-DPB1*89:01',
         'HLA-DPA1*01:09-DPB1*90:01', 'HLA-DPA1*01:09-DPB1*91:01', 'HLA-DPA1*01:09-DPB1*92:01', 'HLA-DPA1*01:09-DPB1*93:01',
         'HLA-DPA1*01:09-DPB1*94:01', 'HLA-DPA1*01:09-DPB1*95:01',
         'HLA-DPA1*01:09-DPB1*96:01', 'HLA-DPA1*01:09-DPB1*97:01', 'HLA-DPA1*01:09-DPB1*98:01', 'HLA-DPA1*01:09-DPB1*99:01',
         'HLA-DPA1*01:10-DPB1*01:01', 'HLA-DPA1*01:10-DPB1*02:01',
         'HLA-DPA1*01:10-DPB1*02:02', 'HLA-DPA1*01:10-DPB1*03:01', 'HLA-DPA1*01:10-DPB1*04:01', 'HLA-DPA1*01:10-DPB1*04:02',
         'HLA-DPA1*01:10-DPB1*05:01', 'HLA-DPA1*01:10-DPB1*06:01',
         'HLA-DPA1*01:10-DPB1*08:01', 'HLA-DPA1*01:10-DPB1*09:01', 'HLA-DPA1*01:10-DPB1*10:001', 'HLA-DPA1*01:10-DPB1*10:01',
         'HLA-DPA1*01:10-DPB1*10:101', 'HLA-DPA1*01:10-DPB1*10:201',
         'HLA-DPA1*01:10-DPB1*10:301', 'HLA-DPA1*01:10-DPB1*10:401', 'HLA-DPA1*01:10-DPB1*10:501', 'HLA-DPA1*01:10-DPB1*10:601',
         'HLA-DPA1*01:10-DPB1*10:701', 'HLA-DPA1*01:10-DPB1*10:801',
         'HLA-DPA1*01:10-DPB1*10:901', 'HLA-DPA1*01:10-DPB1*11:001', 'HLA-DPA1*01:10-DPB1*11:01', 'HLA-DPA1*01:10-DPB1*11:101',
         'HLA-DPA1*01:10-DPB1*11:201', 'HLA-DPA1*01:10-DPB1*11:301',
         'HLA-DPA1*01:10-DPB1*11:401', 'HLA-DPA1*01:10-DPB1*11:501', 'HLA-DPA1*01:10-DPB1*11:601', 'HLA-DPA1*01:10-DPB1*11:701',
         'HLA-DPA1*01:10-DPB1*11:801', 'HLA-DPA1*01:10-DPB1*11:901',
         'HLA-DPA1*01:10-DPB1*12:101', 'HLA-DPA1*01:10-DPB1*12:201', 'HLA-DPA1*01:10-DPB1*12:301', 'HLA-DPA1*01:10-DPB1*12:401',
         'HLA-DPA1*01:10-DPB1*12:501', 'HLA-DPA1*01:10-DPB1*12:601',
         'HLA-DPA1*01:10-DPB1*12:701', 'HLA-DPA1*01:10-DPB1*12:801', 'HLA-DPA1*01:10-DPB1*12:901', 'HLA-DPA1*01:10-DPB1*13:001',
         'HLA-DPA1*01:10-DPB1*13:01', 'HLA-DPA1*01:10-DPB1*13:101',
         'HLA-DPA1*01:10-DPB1*13:201', 'HLA-DPA1*01:10-DPB1*13:301', 'HLA-DPA1*01:10-DPB1*13:401', 'HLA-DPA1*01:10-DPB1*14:01',
         'HLA-DPA1*01:10-DPB1*15:01', 'HLA-DPA1*01:10-DPB1*16:01',
         'HLA-DPA1*01:10-DPB1*17:01', 'HLA-DPA1*01:10-DPB1*18:01', 'HLA-DPA1*01:10-DPB1*19:01', 'HLA-DPA1*01:10-DPB1*20:01',
         'HLA-DPA1*01:10-DPB1*21:01', 'HLA-DPA1*01:10-DPB1*22:01',
         'HLA-DPA1*01:10-DPB1*23:01', 'HLA-DPA1*01:10-DPB1*24:01', 'HLA-DPA1*01:10-DPB1*25:01', 'HLA-DPA1*01:10-DPB1*26:01',
         'HLA-DPA1*01:10-DPB1*27:01', 'HLA-DPA1*01:10-DPB1*28:01',
         'HLA-DPA1*01:10-DPB1*29:01', 'HLA-DPA1*01:10-DPB1*30:01', 'HLA-DPA1*01:10-DPB1*31:01', 'HLA-DPA1*01:10-DPB1*32:01',
         'HLA-DPA1*01:10-DPB1*33:01', 'HLA-DPA1*01:10-DPB1*34:01',
         'HLA-DPA1*01:10-DPB1*35:01', 'HLA-DPA1*01:10-DPB1*36:01', 'HLA-DPA1*01:10-DPB1*37:01', 'HLA-DPA1*01:10-DPB1*38:01',
         'HLA-DPA1*01:10-DPB1*39:01', 'HLA-DPA1*01:10-DPB1*40:01',
         'HLA-DPA1*01:10-DPB1*41:01', 'HLA-DPA1*01:10-DPB1*44:01', 'HLA-DPA1*01:10-DPB1*45:01', 'HLA-DPA1*01:10-DPB1*46:01',
         'HLA-DPA1*01:10-DPB1*47:01', 'HLA-DPA1*01:10-DPB1*48:01',
         'HLA-DPA1*01:10-DPB1*49:01', 'HLA-DPA1*01:10-DPB1*50:01', 'HLA-DPA1*01:10-DPB1*51:01', 'HLA-DPA1*01:10-DPB1*52:01',
         'HLA-DPA1*01:10-DPB1*53:01', 'HLA-DPA1*01:10-DPB1*54:01',
         'HLA-DPA1*01:10-DPB1*55:01', 'HLA-DPA1*01:10-DPB1*56:01', 'HLA-DPA1*01:10-DPB1*58:01', 'HLA-DPA1*01:10-DPB1*59:01',
         'HLA-DPA1*01:10-DPB1*60:01', 'HLA-DPA1*01:10-DPB1*62:01',
         'HLA-DPA1*01:10-DPB1*63:01', 'HLA-DPA1*01:10-DPB1*65:01', 'HLA-DPA1*01:10-DPB1*66:01', 'HLA-DPA1*01:10-DPB1*67:01',
         'HLA-DPA1*01:10-DPB1*68:01', 'HLA-DPA1*01:10-DPB1*69:01',
         'HLA-DPA1*01:10-DPB1*70:01', 'HLA-DPA1*01:10-DPB1*71:01', 'HLA-DPA1*01:10-DPB1*72:01', 'HLA-DPA1*01:10-DPB1*73:01',
         'HLA-DPA1*01:10-DPB1*74:01', 'HLA-DPA1*01:10-DPB1*75:01',
         'HLA-DPA1*01:10-DPB1*76:01', 'HLA-DPA1*01:10-DPB1*77:01', 'HLA-DPA1*01:10-DPB1*78:01', 'HLA-DPA1*01:10-DPB1*79:01',
         'HLA-DPA1*01:10-DPB1*80:01', 'HLA-DPA1*01:10-DPB1*81:01',
         'HLA-DPA1*01:10-DPB1*82:01', 'HLA-DPA1*01:10-DPB1*83:01', 'HLA-DPA1*01:10-DPB1*84:01', 'HLA-DPA1*01:10-DPB1*85:01',
         'HLA-DPA1*01:10-DPB1*86:01', 'HLA-DPA1*01:10-DPB1*87:01',
         'HLA-DPA1*01:10-DPB1*88:01', 'HLA-DPA1*01:10-DPB1*89:01', 'HLA-DPA1*01:10-DPB1*90:01', 'HLA-DPA1*01:10-DPB1*91:01',
         'HLA-DPA1*01:10-DPB1*92:01', 'HLA-DPA1*01:10-DPB1*93:01',
         'HLA-DPA1*01:10-DPB1*94:01', 'HLA-DPA1*01:10-DPB1*95:01', 'HLA-DPA1*01:10-DPB1*96:01', 'HLA-DPA1*01:10-DPB1*97:01',
         'HLA-DPA1*01:10-DPB1*98:01', 'HLA-DPA1*01:10-DPB1*99:01',
         'HLA-DPA1*02:01-DPB1*01:01', 'HLA-DPA1*02:01-DPB1*02:01', 'HLA-DPA1*02:01-DPB1*02:02', 'HLA-DPA1*02:01-DPB1*03:01',
         'HLA-DPA1*02:01-DPB1*04:01', 'HLA-DPA1*02:01-DPB1*04:02',
         'HLA-DPA1*02:01-DPB1*05:01', 'HLA-DPA1*02:01-DPB1*06:01', 'HLA-DPA1*02:01-DPB1*08:01', 'HLA-DPA1*02:01-DPB1*09:01',
         'HLA-DPA1*02:01-DPB1*10:001', 'HLA-DPA1*02:01-DPB1*10:01',
         'HLA-DPA1*02:01-DPB1*10:101', 'HLA-DPA1*02:01-DPB1*10:201', 'HLA-DPA1*02:01-DPB1*10:301', 'HLA-DPA1*02:01-DPB1*10:401',
         'HLA-DPA1*02:01-DPB1*10:501', 'HLA-DPA1*02:01-DPB1*10:601',
         'HLA-DPA1*02:01-DPB1*10:701', 'HLA-DPA1*02:01-DPB1*10:801', 'HLA-DPA1*02:01-DPB1*10:901', 'HLA-DPA1*02:01-DPB1*11:001',
         'HLA-DPA1*02:01-DPB1*11:01', 'HLA-DPA1*02:01-DPB1*11:101',
         'HLA-DPA1*02:01-DPB1*11:201', 'HLA-DPA1*02:01-DPB1*11:301', 'HLA-DPA1*02:01-DPB1*11:401', 'HLA-DPA1*02:01-DPB1*11:501',
         'HLA-DPA1*02:01-DPB1*11:601', 'HLA-DPA1*02:01-DPB1*11:701',
         'HLA-DPA1*02:01-DPB1*11:801', 'HLA-DPA1*02:01-DPB1*11:901', 'HLA-DPA1*02:01-DPB1*12:101', 'HLA-DPA1*02:01-DPB1*12:201',
         'HLA-DPA1*02:01-DPB1*12:301', 'HLA-DPA1*02:01-DPB1*12:401',
         'HLA-DPA1*02:01-DPB1*12:501', 'HLA-DPA1*02:01-DPB1*12:601', 'HLA-DPA1*02:01-DPB1*12:701', 'HLA-DPA1*02:01-DPB1*12:801',
         'HLA-DPA1*02:01-DPB1*12:901', 'HLA-DPA1*02:01-DPB1*13:001',
         'HLA-DPA1*02:01-DPB1*13:01', 'HLA-DPA1*02:01-DPB1*13:101', 'HLA-DPA1*02:01-DPB1*13:201', 'HLA-DPA1*02:01-DPB1*13:301',
         'HLA-DPA1*02:01-DPB1*13:401', 'HLA-DPA1*02:01-DPB1*14:01',
         'HLA-DPA1*02:01-DPB1*15:01', 'HLA-DPA1*02:01-DPB1*16:01', 'HLA-DPA1*02:01-DPB1*17:01', 'HLA-DPA1*02:01-DPB1*18:01',
         'HLA-DPA1*02:01-DPB1*19:01', 'HLA-DPA1*02:01-DPB1*20:01',
         'HLA-DPA1*02:01-DPB1*21:01', 'HLA-DPA1*02:01-DPB1*22:01', 'HLA-DPA1*02:01-DPB1*23:01', 'HLA-DPA1*02:01-DPB1*24:01',
         'HLA-DPA1*02:01-DPB1*25:01', 'HLA-DPA1*02:01-DPB1*26:01',
         'HLA-DPA1*02:01-DPB1*27:01', 'HLA-DPA1*02:01-DPB1*28:01', 'HLA-DPA1*02:01-DPB1*29:01', 'HLA-DPA1*02:01-DPB1*30:01',
         'HLA-DPA1*02:01-DPB1*31:01', 'HLA-DPA1*02:01-DPB1*32:01',
         'HLA-DPA1*02:01-DPB1*33:01', 'HLA-DPA1*02:01-DPB1*34:01', 'HLA-DPA1*02:01-DPB1*35:01', 'HLA-DPA1*02:01-DPB1*36:01',
         'HLA-DPA1*02:01-DPB1*37:01', 'HLA-DPA1*02:01-DPB1*38:01',
         'HLA-DPA1*02:01-DPB1*39:01', 'HLA-DPA1*02:01-DPB1*40:01', 'HLA-DPA1*02:01-DPB1*41:01', 'HLA-DPA1*02:01-DPB1*44:01',
         'HLA-DPA1*02:01-DPB1*45:01', 'HLA-DPA1*02:01-DPB1*46:01',
         'HLA-DPA1*02:01-DPB1*47:01', 'HLA-DPA1*02:01-DPB1*48:01', 'HLA-DPA1*02:01-DPB1*49:01', 'HLA-DPA1*02:01-DPB1*50:01',
         'HLA-DPA1*02:01-DPB1*51:01', 'HLA-DPA1*02:01-DPB1*52:01',
         'HLA-DPA1*02:01-DPB1*53:01', 'HLA-DPA1*02:01-DPB1*54:01', 'HLA-DPA1*02:01-DPB1*55:01', 'HLA-DPA1*02:01-DPB1*56:01',
         'HLA-DPA1*02:01-DPB1*58:01', 'HLA-DPA1*02:01-DPB1*59:01',
         'HLA-DPA1*02:01-DPB1*60:01', 'HLA-DPA1*02:01-DPB1*62:01', 'HLA-DPA1*02:01-DPB1*63:01', 'HLA-DPA1*02:01-DPB1*65:01',
         'HLA-DPA1*02:01-DPB1*66:01', 'HLA-DPA1*02:01-DPB1*67:01',
         'HLA-DPA1*02:01-DPB1*68:01', 'HLA-DPA1*02:01-DPB1*69:01', 'HLA-DPA1*02:01-DPB1*70:01', 'HLA-DPA1*02:01-DPB1*71:01',
         'HLA-DPA1*02:01-DPB1*72:01', 'HLA-DPA1*02:01-DPB1*73:01',
         'HLA-DPA1*02:01-DPB1*74:01', 'HLA-DPA1*02:01-DPB1*75:01', 'HLA-DPA1*02:01-DPB1*76:01', 'HLA-DPA1*02:01-DPB1*77:01',
         'HLA-DPA1*02:01-DPB1*78:01', 'HLA-DPA1*02:01-DPB1*79:01',
         'HLA-DPA1*02:01-DPB1*80:01', 'HLA-DPA1*02:01-DPB1*81:01', 'HLA-DPA1*02:01-DPB1*82:01', 'HLA-DPA1*02:01-DPB1*83:01',
         'HLA-DPA1*02:01-DPB1*84:01', 'HLA-DPA1*02:01-DPB1*85:01',
         'HLA-DPA1*02:01-DPB1*86:01', 'HLA-DPA1*02:01-DPB1*87:01', 'HLA-DPA1*02:01-DPB1*88:01', 'HLA-DPA1*02:01-DPB1*89:01',
         'HLA-DPA1*02:01-DPB1*90:01', 'HLA-DPA1*02:01-DPB1*91:01',
         'HLA-DPA1*02:01-DPB1*92:01', 'HLA-DPA1*02:01-DPB1*93:01', 'HLA-DPA1*02:01-DPB1*94:01', 'HLA-DPA1*02:01-DPB1*95:01',
         'HLA-DPA1*02:01-DPB1*96:01', 'HLA-DPA1*02:01-DPB1*97:01',
         'HLA-DPA1*02:01-DPB1*98:01', 'HLA-DPA1*02:01-DPB1*99:01', 'HLA-DPA1*02:02-DPB1*01:01', 'HLA-DPA1*02:02-DPB1*02:01',
         'HLA-DPA1*02:02-DPB1*02:02', 'HLA-DPA1*02:02-DPB1*03:01',
         'HLA-DPA1*02:02-DPB1*04:01', 'HLA-DPA1*02:02-DPB1*04:02', 'HLA-DPA1*02:02-DPB1*05:01', 'HLA-DPA1*02:02-DPB1*06:01',
         'HLA-DPA1*02:02-DPB1*08:01', 'HLA-DPA1*02:02-DPB1*09:01',
         'HLA-DPA1*02:02-DPB1*10:001', 'HLA-DPA1*02:02-DPB1*10:01', 'HLA-DPA1*02:02-DPB1*10:101', 'HLA-DPA1*02:02-DPB1*10:201',
         'HLA-DPA1*02:02-DPB1*10:301', 'HLA-DPA1*02:02-DPB1*10:401',
         'HLA-DPA1*02:02-DPB1*10:501', 'HLA-DPA1*02:02-DPB1*10:601', 'HLA-DPA1*02:02-DPB1*10:701', 'HLA-DPA1*02:02-DPB1*10:801',
         'HLA-DPA1*02:02-DPB1*10:901', 'HLA-DPA1*02:02-DPB1*11:001',
         'HLA-DPA1*02:02-DPB1*11:01', 'HLA-DPA1*02:02-DPB1*11:101', 'HLA-DPA1*02:02-DPB1*11:201', 'HLA-DPA1*02:02-DPB1*11:301',
         'HLA-DPA1*02:02-DPB1*11:401', 'HLA-DPA1*02:02-DPB1*11:501',
         'HLA-DPA1*02:02-DPB1*11:601', 'HLA-DPA1*02:02-DPB1*11:701', 'HLA-DPA1*02:02-DPB1*11:801', 'HLA-DPA1*02:02-DPB1*11:901',
         'HLA-DPA1*02:02-DPB1*12:101', 'HLA-DPA1*02:02-DPB1*12:201',
         'HLA-DPA1*02:02-DPB1*12:301', 'HLA-DPA1*02:02-DPB1*12:401', 'HLA-DPA1*02:02-DPB1*12:501', 'HLA-DPA1*02:02-DPB1*12:601',
         'HLA-DPA1*02:02-DPB1*12:701', 'HLA-DPA1*02:02-DPB1*12:801',
         'HLA-DPA1*02:02-DPB1*12:901', 'HLA-DPA1*02:02-DPB1*13:001', 'HLA-DPA1*02:02-DPB1*13:01', 'HLA-DPA1*02:02-DPB1*13:101',
         'HLA-DPA1*02:02-DPB1*13:201', 'HLA-DPA1*02:02-DPB1*13:301',
         'HLA-DPA1*02:02-DPB1*13:401', 'HLA-DPA1*02:02-DPB1*14:01', 'HLA-DPA1*02:02-DPB1*15:01', 'HLA-DPA1*02:02-DPB1*16:01',
         'HLA-DPA1*02:02-DPB1*17:01', 'HLA-DPA1*02:02-DPB1*18:01',
         'HLA-DPA1*02:02-DPB1*19:01', 'HLA-DPA1*02:02-DPB1*20:01', 'HLA-DPA1*02:02-DPB1*21:01', 'HLA-DPA1*02:02-DPB1*22:01',
         'HLA-DPA1*02:02-DPB1*23:01', 'HLA-DPA1*02:02-DPB1*24:01',
         'HLA-DPA1*02:02-DPB1*25:01', 'HLA-DPA1*02:02-DPB1*26:01', 'HLA-DPA1*02:02-DPB1*27:01', 'HLA-DPA1*02:02-DPB1*28:01',
         'HLA-DPA1*02:02-DPB1*29:01', 'HLA-DPA1*02:02-DPB1*30:01',
         'HLA-DPA1*02:02-DPB1*31:01', 'HLA-DPA1*02:02-DPB1*32:01', 'HLA-DPA1*02:02-DPB1*33:01', 'HLA-DPA1*02:02-DPB1*34:01',
         'HLA-DPA1*02:02-DPB1*35:01', 'HLA-DPA1*02:02-DPB1*36:01',
         'HLA-DPA1*02:02-DPB1*37:01', 'HLA-DPA1*02:02-DPB1*38:01', 'HLA-DPA1*02:02-DPB1*39:01', 'HLA-DPA1*02:02-DPB1*40:01',
         'HLA-DPA1*02:02-DPB1*41:01', 'HLA-DPA1*02:02-DPB1*44:01',
         'HLA-DPA1*02:02-DPB1*45:01', 'HLA-DPA1*02:02-DPB1*46:01', 'HLA-DPA1*02:02-DPB1*47:01', 'HLA-DPA1*02:02-DPB1*48:01',
         'HLA-DPA1*02:02-DPB1*49:01', 'HLA-DPA1*02:02-DPB1*50:01',
         'HLA-DPA1*02:02-DPB1*51:01', 'HLA-DPA1*02:02-DPB1*52:01', 'HLA-DPA1*02:02-DPB1*53:01', 'HLA-DPA1*02:02-DPB1*54:01',
         'HLA-DPA1*02:02-DPB1*55:01', 'HLA-DPA1*02:02-DPB1*56:01',
         'HLA-DPA1*02:02-DPB1*58:01', 'HLA-DPA1*02:02-DPB1*59:01', 'HLA-DPA1*02:02-DPB1*60:01', 'HLA-DPA1*02:02-DPB1*62:01',
         'HLA-DPA1*02:02-DPB1*63:01', 'HLA-DPA1*02:02-DPB1*65:01',
         'HLA-DPA1*02:02-DPB1*66:01', 'HLA-DPA1*02:02-DPB1*67:01', 'HLA-DPA1*02:02-DPB1*68:01', 'HLA-DPA1*02:02-DPB1*69:01',
         'HLA-DPA1*02:02-DPB1*70:01', 'HLA-DPA1*02:02-DPB1*71:01',
         'HLA-DPA1*02:02-DPB1*72:01', 'HLA-DPA1*02:02-DPB1*73:01', 'HLA-DPA1*02:02-DPB1*74:01', 'HLA-DPA1*02:02-DPB1*75:01',
         'HLA-DPA1*02:02-DPB1*76:01', 'HLA-DPA1*02:02-DPB1*77:01',
         'HLA-DPA1*02:02-DPB1*78:01', 'HLA-DPA1*02:02-DPB1*79:01', 'HLA-DPA1*02:02-DPB1*80:01', 'HLA-DPA1*02:02-DPB1*81:01',
         'HLA-DPA1*02:02-DPB1*82:01', 'HLA-DPA1*02:02-DPB1*83:01',
         'HLA-DPA1*02:02-DPB1*84:01', 'HLA-DPA1*02:02-DPB1*85:01', 'HLA-DPA1*02:02-DPB1*86:01', 'HLA-DPA1*02:02-DPB1*87:01',
         'HLA-DPA1*02:02-DPB1*88:01', 'HLA-DPA1*02:02-DPB1*89:01',
         'HLA-DPA1*02:02-DPB1*90:01', 'HLA-DPA1*02:02-DPB1*91:01', 'HLA-DPA1*02:02-DPB1*92:01', 'HLA-DPA1*02:02-DPB1*93:01',
         'HLA-DPA1*02:02-DPB1*94:01', 'HLA-DPA1*02:02-DPB1*95:01',
         'HLA-DPA1*02:02-DPB1*96:01', 'HLA-DPA1*02:02-DPB1*97:01', 'HLA-DPA1*02:02-DPB1*98:01', 'HLA-DPA1*02:02-DPB1*99:01',
         'HLA-DPA1*02:03-DPB1*01:01', 'HLA-DPA1*02:03-DPB1*02:01',
         'HLA-DPA1*02:03-DPB1*02:02', 'HLA-DPA1*02:03-DPB1*03:01', 'HLA-DPA1*02:03-DPB1*04:01', 'HLA-DPA1*02:03-DPB1*04:02',
         'HLA-DPA1*02:03-DPB1*05:01', 'HLA-DPA1*02:03-DPB1*06:01',
         'HLA-DPA1*02:03-DPB1*08:01', 'HLA-DPA1*02:03-DPB1*09:01', 'HLA-DPA1*02:03-DPB1*10:001', 'HLA-DPA1*02:03-DPB1*10:01',
         'HLA-DPA1*02:03-DPB1*10:101', 'HLA-DPA1*02:03-DPB1*10:201',
         'HLA-DPA1*02:03-DPB1*10:301', 'HLA-DPA1*02:03-DPB1*10:401', 'HLA-DPA1*02:03-DPB1*10:501', 'HLA-DPA1*02:03-DPB1*10:601',
         'HLA-DPA1*02:03-DPB1*10:701', 'HLA-DPA1*02:03-DPB1*10:801',
         'HLA-DPA1*02:03-DPB1*10:901', 'HLA-DPA1*02:03-DPB1*11:001', 'HLA-DPA1*02:03-DPB1*11:01', 'HLA-DPA1*02:03-DPB1*11:101',
         'HLA-DPA1*02:03-DPB1*11:201', 'HLA-DPA1*02:03-DPB1*11:301',
         'HLA-DPA1*02:03-DPB1*11:401', 'HLA-DPA1*02:03-DPB1*11:501', 'HLA-DPA1*02:03-DPB1*11:601', 'HLA-DPA1*02:03-DPB1*11:701',
         'HLA-DPA1*02:03-DPB1*11:801', 'HLA-DPA1*02:03-DPB1*11:901',
         'HLA-DPA1*02:03-DPB1*12:101', 'HLA-DPA1*02:03-DPB1*12:201', 'HLA-DPA1*02:03-DPB1*12:301', 'HLA-DPA1*02:03-DPB1*12:401',
         'HLA-DPA1*02:03-DPB1*12:501', 'HLA-DPA1*02:03-DPB1*12:601',
         'HLA-DPA1*02:03-DPB1*12:701', 'HLA-DPA1*02:03-DPB1*12:801', 'HLA-DPA1*02:03-DPB1*12:901', 'HLA-DPA1*02:03-DPB1*13:001',
         'HLA-DPA1*02:03-DPB1*13:01', 'HLA-DPA1*02:03-DPB1*13:101',
         'HLA-DPA1*02:03-DPB1*13:201', 'HLA-DPA1*02:03-DPB1*13:301', 'HLA-DPA1*02:03-DPB1*13:401', 'HLA-DPA1*02:03-DPB1*14:01',
         'HLA-DPA1*02:03-DPB1*15:01', 'HLA-DPA1*02:03-DPB1*16:01',
         'HLA-DPA1*02:03-DPB1*17:01', 'HLA-DPA1*02:03-DPB1*18:01', 'HLA-DPA1*02:03-DPB1*19:01', 'HLA-DPA1*02:03-DPB1*20:01',
         'HLA-DPA1*02:03-DPB1*21:01', 'HLA-DPA1*02:03-DPB1*22:01',
         'HLA-DPA1*02:03-DPB1*23:01', 'HLA-DPA1*02:03-DPB1*24:01', 'HLA-DPA1*02:03-DPB1*25:01', 'HLA-DPA1*02:03-DPB1*26:01',
         'HLA-DPA1*02:03-DPB1*27:01', 'HLA-DPA1*02:03-DPB1*28:01',
         'HLA-DPA1*02:03-DPB1*29:01', 'HLA-DPA1*02:03-DPB1*30:01', 'HLA-DPA1*02:03-DPB1*31:01', 'HLA-DPA1*02:03-DPB1*32:01',
         'HLA-DPA1*02:03-DPB1*33:01', 'HLA-DPA1*02:03-DPB1*34:01',
         'HLA-DPA1*02:03-DPB1*35:01', 'HLA-DPA1*02:03-DPB1*36:01', 'HLA-DPA1*02:03-DPB1*37:01', 'HLA-DPA1*02:03-DPB1*38:01',
         'HLA-DPA1*02:03-DPB1*39:01', 'HLA-DPA1*02:03-DPB1*40:01',
         'HLA-DPA1*02:03-DPB1*41:01', 'HLA-DPA1*02:03-DPB1*44:01', 'HLA-DPA1*02:03-DPB1*45:01', 'HLA-DPA1*02:03-DPB1*46:01',
         'HLA-DPA1*02:03-DPB1*47:01', 'HLA-DPA1*02:03-DPB1*48:01',
         'HLA-DPA1*02:03-DPB1*49:01', 'HLA-DPA1*02:03-DPB1*50:01', 'HLA-DPA1*02:03-DPB1*51:01', 'HLA-DPA1*02:03-DPB1*52:01',
         'HLA-DPA1*02:03-DPB1*53:01', 'HLA-DPA1*02:03-DPB1*54:01',
         'HLA-DPA1*02:03-DPB1*55:01', 'HLA-DPA1*02:03-DPB1*56:01', 'HLA-DPA1*02:03-DPB1*58:01', 'HLA-DPA1*02:03-DPB1*59:01',
         'HLA-DPA1*02:03-DPB1*60:01', 'HLA-DPA1*02:03-DPB1*62:01',
         'HLA-DPA1*02:03-DPB1*63:01', 'HLA-DPA1*02:03-DPB1*65:01', 'HLA-DPA1*02:03-DPB1*66:01', 'HLA-DPA1*02:03-DPB1*67:01',
         'HLA-DPA1*02:03-DPB1*68:01', 'HLA-DPA1*02:03-DPB1*69:01',
         'HLA-DPA1*02:03-DPB1*70:01', 'HLA-DPA1*02:03-DPB1*71:01', 'HLA-DPA1*02:03-DPB1*72:01', 'HLA-DPA1*02:03-DPB1*73:01',
         'HLA-DPA1*02:03-DPB1*74:01', 'HLA-DPA1*02:03-DPB1*75:01',
         'HLA-DPA1*02:03-DPB1*76:01', 'HLA-DPA1*02:03-DPB1*77:01', 'HLA-DPA1*02:03-DPB1*78:01', 'HLA-DPA1*02:03-DPB1*79:01',
         'HLA-DPA1*02:03-DPB1*80:01', 'HLA-DPA1*02:03-DPB1*81:01',
         'HLA-DPA1*02:03-DPB1*82:01', 'HLA-DPA1*02:03-DPB1*83:01', 'HLA-DPA1*02:03-DPB1*84:01', 'HLA-DPA1*02:03-DPB1*85:01',
         'HLA-DPA1*02:03-DPB1*86:01', 'HLA-DPA1*02:03-DPB1*87:01',
         'HLA-DPA1*02:03-DPB1*88:01', 'HLA-DPA1*02:03-DPB1*89:01', 'HLA-DPA1*02:03-DPB1*90:01', 'HLA-DPA1*02:03-DPB1*91:01',
         'HLA-DPA1*02:03-DPB1*92:01', 'HLA-DPA1*02:03-DPB1*93:01',
         'HLA-DPA1*02:03-DPB1*94:01', 'HLA-DPA1*02:03-DPB1*95:01', 'HLA-DPA1*02:03-DPB1*96:01', 'HLA-DPA1*02:03-DPB1*97:01',
         'HLA-DPA1*02:03-DPB1*98:01', 'HLA-DPA1*02:03-DPB1*99:01',
         'HLA-DPA1*02:04-DPB1*01:01', 'HLA-DPA1*02:04-DPB1*02:01', 'HLA-DPA1*02:04-DPB1*02:02', 'HLA-DPA1*02:04-DPB1*03:01',
         'HLA-DPA1*02:04-DPB1*04:01', 'HLA-DPA1*02:04-DPB1*04:02',
         'HLA-DPA1*02:04-DPB1*05:01', 'HLA-DPA1*02:04-DPB1*06:01', 'HLA-DPA1*02:04-DPB1*08:01', 'HLA-DPA1*02:04-DPB1*09:01',
         'HLA-DPA1*02:04-DPB1*10:001', 'HLA-DPA1*02:04-DPB1*10:01',
         'HLA-DPA1*02:04-DPB1*10:101', 'HLA-DPA1*02:04-DPB1*10:201', 'HLA-DPA1*02:04-DPB1*10:301', 'HLA-DPA1*02:04-DPB1*10:401',
         'HLA-DPA1*02:04-DPB1*10:501', 'HLA-DPA1*02:04-DPB1*10:601',
         'HLA-DPA1*02:04-DPB1*10:701', 'HLA-DPA1*02:04-DPB1*10:801', 'HLA-DPA1*02:04-DPB1*10:901', 'HLA-DPA1*02:04-DPB1*11:001',
         'HLA-DPA1*02:04-DPB1*11:01', 'HLA-DPA1*02:04-DPB1*11:101',
         'HLA-DPA1*02:04-DPB1*11:201', 'HLA-DPA1*02:04-DPB1*11:301', 'HLA-DPA1*02:04-DPB1*11:401', 'HLA-DPA1*02:04-DPB1*11:501',
         'HLA-DPA1*02:04-DPB1*11:601', 'HLA-DPA1*02:04-DPB1*11:701',
         'HLA-DPA1*02:04-DPB1*11:801', 'HLA-DPA1*02:04-DPB1*11:901', 'HLA-DPA1*02:04-DPB1*12:101', 'HLA-DPA1*02:04-DPB1*12:201',
         'HLA-DPA1*02:04-DPB1*12:301', 'HLA-DPA1*02:04-DPB1*12:401',
         'HLA-DPA1*02:04-DPB1*12:501', 'HLA-DPA1*02:04-DPB1*12:601', 'HLA-DPA1*02:04-DPB1*12:701', 'HLA-DPA1*02:04-DPB1*12:801',
         'HLA-DPA1*02:04-DPB1*12:901', 'HLA-DPA1*02:04-DPB1*13:001',
         'HLA-DPA1*02:04-DPB1*13:01', 'HLA-DPA1*02:04-DPB1*13:101', 'HLA-DPA1*02:04-DPB1*13:201', 'HLA-DPA1*02:04-DPB1*13:301',
         'HLA-DPA1*02:04-DPB1*13:401', 'HLA-DPA1*02:04-DPB1*14:01',
         'HLA-DPA1*02:04-DPB1*15:01', 'HLA-DPA1*02:04-DPB1*16:01', 'HLA-DPA1*02:04-DPB1*17:01', 'HLA-DPA1*02:04-DPB1*18:01',
         'HLA-DPA1*02:04-DPB1*19:01', 'HLA-DPA1*02:04-DPB1*20:01',
         'HLA-DPA1*02:04-DPB1*21:01', 'HLA-DPA1*02:04-DPB1*22:01', 'HLA-DPA1*02:04-DPB1*23:01', 'HLA-DPA1*02:04-DPB1*24:01',
         'HLA-DPA1*02:04-DPB1*25:01', 'HLA-DPA1*02:04-DPB1*26:01',
         'HLA-DPA1*02:04-DPB1*27:01', 'HLA-DPA1*02:04-DPB1*28:01', 'HLA-DPA1*02:04-DPB1*29:01', 'HLA-DPA1*02:04-DPB1*30:01',
         'HLA-DPA1*02:04-DPB1*31:01', 'HLA-DPA1*02:04-DPB1*32:01',
         'HLA-DPA1*02:04-DPB1*33:01', 'HLA-DPA1*02:04-DPB1*34:01', 'HLA-DPA1*02:04-DPB1*35:01', 'HLA-DPA1*02:04-DPB1*36:01',
         'HLA-DPA1*02:04-DPB1*37:01', 'HLA-DPA1*02:04-DPB1*38:01',
         'HLA-DPA1*02:04-DPB1*39:01', 'HLA-DPA1*02:04-DPB1*40:01', 'HLA-DPA1*02:04-DPB1*41:01', 'HLA-DPA1*02:04-DPB1*44:01',
         'HLA-DPA1*02:04-DPB1*45:01', 'HLA-DPA1*02:04-DPB1*46:01',
         'HLA-DPA1*02:04-DPB1*47:01', 'HLA-DPA1*02:04-DPB1*48:01', 'HLA-DPA1*02:04-DPB1*49:01', 'HLA-DPA1*02:04-DPB1*50:01',
         'HLA-DPA1*02:04-DPB1*51:01', 'HLA-DPA1*02:04-DPB1*52:01',
         'HLA-DPA1*02:04-DPB1*53:01', 'HLA-DPA1*02:04-DPB1*54:01', 'HLA-DPA1*02:04-DPB1*55:01', 'HLA-DPA1*02:04-DPB1*56:01',
         'HLA-DPA1*02:04-DPB1*58:01', 'HLA-DPA1*02:04-DPB1*59:01',
         'HLA-DPA1*02:04-DPB1*60:01', 'HLA-DPA1*02:04-DPB1*62:01', 'HLA-DPA1*02:04-DPB1*63:01', 'HLA-DPA1*02:04-DPB1*65:01',
         'HLA-DPA1*02:04-DPB1*66:01', 'HLA-DPA1*02:04-DPB1*67:01',
         'HLA-DPA1*02:04-DPB1*68:01', 'HLA-DPA1*02:04-DPB1*69:01', 'HLA-DPA1*02:04-DPB1*70:01', 'HLA-DPA1*02:04-DPB1*71:01',
         'HLA-DPA1*02:04-DPB1*72:01', 'HLA-DPA1*02:04-DPB1*73:01',
         'HLA-DPA1*02:04-DPB1*74:01', 'HLA-DPA1*02:04-DPB1*75:01', 'HLA-DPA1*02:04-DPB1*76:01', 'HLA-DPA1*02:04-DPB1*77:01',
         'HLA-DPA1*02:04-DPB1*78:01', 'HLA-DPA1*02:04-DPB1*79:01',
         'HLA-DPA1*02:04-DPB1*80:01', 'HLA-DPA1*02:04-DPB1*81:01', 'HLA-DPA1*02:04-DPB1*82:01', 'HLA-DPA1*02:04-DPB1*83:01',
         'HLA-DPA1*02:04-DPB1*84:01', 'HLA-DPA1*02:04-DPB1*85:01',
         'HLA-DPA1*02:04-DPB1*86:01', 'HLA-DPA1*02:04-DPB1*87:01', 'HLA-DPA1*02:04-DPB1*88:01', 'HLA-DPA1*02:04-DPB1*89:01',
         'HLA-DPA1*02:04-DPB1*90:01', 'HLA-DPA1*02:04-DPB1*91:01',
         'HLA-DPA1*02:04-DPB1*92:01', 'HLA-DPA1*02:04-DPB1*93:01', 'HLA-DPA1*02:04-DPB1*94:01', 'HLA-DPA1*02:04-DPB1*95:01',
         'HLA-DPA1*02:04-DPB1*96:01', 'HLA-DPA1*02:04-DPB1*97:01',
         'HLA-DPA1*02:04-DPB1*98:01', 'HLA-DPA1*02:04-DPB1*99:01', 'HLA-DPA1*03:01-DPB1*01:01', 'HLA-DPA1*03:01-DPB1*02:01',
         'HLA-DPA1*03:01-DPB1*02:02', 'HLA-DPA1*03:01-DPB1*03:01',
         'HLA-DPA1*03:01-DPB1*04:01', 'HLA-DPA1*03:01-DPB1*04:02', 'HLA-DPA1*03:01-DPB1*05:01', 'HLA-DPA1*03:01-DPB1*06:01',
         'HLA-DPA1*03:01-DPB1*08:01', 'HLA-DPA1*03:01-DPB1*09:01',
         'HLA-DPA1*03:01-DPB1*10:001', 'HLA-DPA1*03:01-DPB1*10:01', 'HLA-DPA1*03:01-DPB1*10:101', 'HLA-DPA1*03:01-DPB1*10:201',
         'HLA-DPA1*03:01-DPB1*10:301', 'HLA-DPA1*03:01-DPB1*10:401',
         'HLA-DPA1*03:01-DPB1*10:501', 'HLA-DPA1*03:01-DPB1*10:601', 'HLA-DPA1*03:01-DPB1*10:701', 'HLA-DPA1*03:01-DPB1*10:801',
         'HLA-DPA1*03:01-DPB1*10:901', 'HLA-DPA1*03:01-DPB1*11:001',
         'HLA-DPA1*03:01-DPB1*11:01', 'HLA-DPA1*03:01-DPB1*11:101', 'HLA-DPA1*03:01-DPB1*11:201', 'HLA-DPA1*03:01-DPB1*11:301',
         'HLA-DPA1*03:01-DPB1*11:401', 'HLA-DPA1*03:01-DPB1*11:501',
         'HLA-DPA1*03:01-DPB1*11:601', 'HLA-DPA1*03:01-DPB1*11:701', 'HLA-DPA1*03:01-DPB1*11:801', 'HLA-DPA1*03:01-DPB1*11:901',
         'HLA-DPA1*03:01-DPB1*12:101', 'HLA-DPA1*03:01-DPB1*12:201',
         'HLA-DPA1*03:01-DPB1*12:301', 'HLA-DPA1*03:01-DPB1*12:401', 'HLA-DPA1*03:01-DPB1*12:501', 'HLA-DPA1*03:01-DPB1*12:601',
         'HLA-DPA1*03:01-DPB1*12:701', 'HLA-DPA1*03:01-DPB1*12:801',
         'HLA-DPA1*03:01-DPB1*12:901', 'HLA-DPA1*03:01-DPB1*13:001', 'HLA-DPA1*03:01-DPB1*13:01', 'HLA-DPA1*03:01-DPB1*13:101',
         'HLA-DPA1*03:01-DPB1*13:201', 'HLA-DPA1*03:01-DPB1*13:301',
         'HLA-DPA1*03:01-DPB1*13:401', 'HLA-DPA1*03:01-DPB1*14:01', 'HLA-DPA1*03:01-DPB1*15:01', 'HLA-DPA1*03:01-DPB1*16:01',
         'HLA-DPA1*03:01-DPB1*17:01', 'HLA-DPA1*03:01-DPB1*18:01',
         'HLA-DPA1*03:01-DPB1*19:01', 'HLA-DPA1*03:01-DPB1*20:01', 'HLA-DPA1*03:01-DPB1*21:01', 'HLA-DPA1*03:01-DPB1*22:01',
         'HLA-DPA1*03:01-DPB1*23:01', 'HLA-DPA1*03:01-DPB1*24:01',
         'HLA-DPA1*03:01-DPB1*25:01', 'HLA-DPA1*03:01-DPB1*26:01', 'HLA-DPA1*03:01-DPB1*27:01', 'HLA-DPA1*03:01-DPB1*28:01',
         'HLA-DPA1*03:01-DPB1*29:01', 'HLA-DPA1*03:01-DPB1*30:01',
         'HLA-DPA1*03:01-DPB1*31:01', 'HLA-DPA1*03:01-DPB1*32:01', 'HLA-DPA1*03:01-DPB1*33:01', 'HLA-DPA1*03:01-DPB1*34:01',
         'HLA-DPA1*03:01-DPB1*35:01', 'HLA-DPA1*03:01-DPB1*36:01',
         'HLA-DPA1*03:01-DPB1*37:01', 'HLA-DPA1*03:01-DPB1*38:01', 'HLA-DPA1*03:01-DPB1*39:01', 'HLA-DPA1*03:01-DPB1*40:01',
         'HLA-DPA1*03:01-DPB1*41:01', 'HLA-DPA1*03:01-DPB1*44:01',
         'HLA-DPA1*03:01-DPB1*45:01', 'HLA-DPA1*03:01-DPB1*46:01', 'HLA-DPA1*03:01-DPB1*47:01', 'HLA-DPA1*03:01-DPB1*48:01',
         'HLA-DPA1*03:01-DPB1*49:01', 'HLA-DPA1*03:01-DPB1*50:01',
         'HLA-DPA1*03:01-DPB1*51:01', 'HLA-DPA1*03:01-DPB1*52:01', 'HLA-DPA1*03:01-DPB1*53:01', 'HLA-DPA1*03:01-DPB1*54:01',
         'HLA-DPA1*03:01-DPB1*55:01', 'HLA-DPA1*03:01-DPB1*56:01',
         'HLA-DPA1*03:01-DPB1*58:01', 'HLA-DPA1*03:01-DPB1*59:01', 'HLA-DPA1*03:01-DPB1*60:01', 'HLA-DPA1*03:01-DPB1*62:01',
         'HLA-DPA1*03:01-DPB1*63:01', 'HLA-DPA1*03:01-DPB1*65:01',
         'HLA-DPA1*03:01-DPB1*66:01', 'HLA-DPA1*03:01-DPB1*67:01', 'HLA-DPA1*03:01-DPB1*68:01', 'HLA-DPA1*03:01-DPB1*69:01',
         'HLA-DPA1*03:01-DPB1*70:01', 'HLA-DPA1*03:01-DPB1*71:01',
         'HLA-DPA1*03:01-DPB1*72:01', 'HLA-DPA1*03:01-DPB1*73:01', 'HLA-DPA1*03:01-DPB1*74:01', 'HLA-DPA1*03:01-DPB1*75:01',
         'HLA-DPA1*03:01-DPB1*76:01', 'HLA-DPA1*03:01-DPB1*77:01',
         'HLA-DPA1*03:01-DPB1*78:01', 'HLA-DPA1*03:01-DPB1*79:01', 'HLA-DPA1*03:01-DPB1*80:01', 'HLA-DPA1*03:01-DPB1*81:01',
         'HLA-DPA1*03:01-DPB1*82:01', 'HLA-DPA1*03:01-DPB1*83:01',
         'HLA-DPA1*03:01-DPB1*84:01', 'HLA-DPA1*03:01-DPB1*85:01', 'HLA-DPA1*03:01-DPB1*86:01', 'HLA-DPA1*03:01-DPB1*87:01',
         'HLA-DPA1*03:01-DPB1*88:01', 'HLA-DPA1*03:01-DPB1*89:01',
         'HLA-DPA1*03:01-DPB1*90:01', 'HLA-DPA1*03:01-DPB1*91:01', 'HLA-DPA1*03:01-DPB1*92:01', 'HLA-DPA1*03:01-DPB1*93:01',
         'HLA-DPA1*03:01-DPB1*94:01', 'HLA-DPA1*03:01-DPB1*95:01',
         'HLA-DPA1*03:01-DPB1*96:01', 'HLA-DPA1*03:01-DPB1*97:01', 'HLA-DPA1*03:01-DPB1*98:01', 'HLA-DPA1*03:01-DPB1*99:01',
         'HLA-DPA1*03:02-DPB1*01:01', 'HLA-DPA1*03:02-DPB1*02:01',
         'HLA-DPA1*03:02-DPB1*02:02', 'HLA-DPA1*03:02-DPB1*03:01', 'HLA-DPA1*03:02-DPB1*04:01', 'HLA-DPA1*03:02-DPB1*04:02',
         'HLA-DPA1*03:02-DPB1*05:01', 'HLA-DPA1*03:02-DPB1*06:01',
         'HLA-DPA1*03:02-DPB1*08:01', 'HLA-DPA1*03:02-DPB1*09:01', 'HLA-DPA1*03:02-DPB1*10:001', 'HLA-DPA1*03:02-DPB1*10:01',
         'HLA-DPA1*03:02-DPB1*10:101', 'HLA-DPA1*03:02-DPB1*10:201',
         'HLA-DPA1*03:02-DPB1*10:301', 'HLA-DPA1*03:02-DPB1*10:401', 'HLA-DPA1*03:02-DPB1*10:501', 'HLA-DPA1*03:02-DPB1*10:601',
         'HLA-DPA1*03:02-DPB1*10:701', 'HLA-DPA1*03:02-DPB1*10:801',
         'HLA-DPA1*03:02-DPB1*10:901', 'HLA-DPA1*03:02-DPB1*11:001', 'HLA-DPA1*03:02-DPB1*11:01', 'HLA-DPA1*03:02-DPB1*11:101',
         'HLA-DPA1*03:02-DPB1*11:201', 'HLA-DPA1*03:02-DPB1*11:301',
         'HLA-DPA1*03:02-DPB1*11:401', 'HLA-DPA1*03:02-DPB1*11:501', 'HLA-DPA1*03:02-DPB1*11:601', 'HLA-DPA1*03:02-DPB1*11:701',
         'HLA-DPA1*03:02-DPB1*11:801', 'HLA-DPA1*03:02-DPB1*11:901',
         'HLA-DPA1*03:02-DPB1*12:101', 'HLA-DPA1*03:02-DPB1*12:201', 'HLA-DPA1*03:02-DPB1*12:301', 'HLA-DPA1*03:02-DPB1*12:401',
         'HLA-DPA1*03:02-DPB1*12:501', 'HLA-DPA1*03:02-DPB1*12:601',
         'HLA-DPA1*03:02-DPB1*12:701', 'HLA-DPA1*03:02-DPB1*12:801', 'HLA-DPA1*03:02-DPB1*12:901', 'HLA-DPA1*03:02-DPB1*13:001',
         'HLA-DPA1*03:02-DPB1*13:01', 'HLA-DPA1*03:02-DPB1*13:101',
         'HLA-DPA1*03:02-DPB1*13:201', 'HLA-DPA1*03:02-DPB1*13:301', 'HLA-DPA1*03:02-DPB1*13:401', 'HLA-DPA1*03:02-DPB1*14:01',
         'HLA-DPA1*03:02-DPB1*15:01', 'HLA-DPA1*03:02-DPB1*16:01',
         'HLA-DPA1*03:02-DPB1*17:01', 'HLA-DPA1*03:02-DPB1*18:01', 'HLA-DPA1*03:02-DPB1*19:01', 'HLA-DPA1*03:02-DPB1*20:01',
         'HLA-DPA1*03:02-DPB1*21:01', 'HLA-DPA1*03:02-DPB1*22:01',
         'HLA-DPA1*03:02-DPB1*23:01', 'HLA-DPA1*03:02-DPB1*24:01', 'HLA-DPA1*03:02-DPB1*25:01', 'HLA-DPA1*03:02-DPB1*26:01',
         'HLA-DPA1*03:02-DPB1*27:01', 'HLA-DPA1*03:02-DPB1*28:01',
         'HLA-DPA1*03:02-DPB1*29:01', 'HLA-DPA1*03:02-DPB1*30:01', 'HLA-DPA1*03:02-DPB1*31:01', 'HLA-DPA1*03:02-DPB1*32:01',
         'HLA-DPA1*03:02-DPB1*33:01', 'HLA-DPA1*03:02-DPB1*34:01',
         'HLA-DPA1*03:02-DPB1*35:01', 'HLA-DPA1*03:02-DPB1*36:01', 'HLA-DPA1*03:02-DPB1*37:01', 'HLA-DPA1*03:02-DPB1*38:01',
         'HLA-DPA1*03:02-DPB1*39:01', 'HLA-DPA1*03:02-DPB1*40:01',
         'HLA-DPA1*03:02-DPB1*41:01', 'HLA-DPA1*03:02-DPB1*44:01', 'HLA-DPA1*03:02-DPB1*45:01', 'HLA-DPA1*03:02-DPB1*46:01',
         'HLA-DPA1*03:02-DPB1*47:01', 'HLA-DPA1*03:02-DPB1*48:01',
         'HLA-DPA1*03:02-DPB1*49:01', 'HLA-DPA1*03:02-DPB1*50:01', 'HLA-DPA1*03:02-DPB1*51:01', 'HLA-DPA1*03:02-DPB1*52:01',
         'HLA-DPA1*03:02-DPB1*53:01', 'HLA-DPA1*03:02-DPB1*54:01',
         'HLA-DPA1*03:02-DPB1*55:01', 'HLA-DPA1*03:02-DPB1*56:01', 'HLA-DPA1*03:02-DPB1*58:01', 'HLA-DPA1*03:02-DPB1*59:01',
         'HLA-DPA1*03:02-DPB1*60:01', 'HLA-DPA1*03:02-DPB1*62:01',
         'HLA-DPA1*03:02-DPB1*63:01', 'HLA-DPA1*03:02-DPB1*65:01', 'HLA-DPA1*03:02-DPB1*66:01', 'HLA-DPA1*03:02-DPB1*67:01',
         'HLA-DPA1*03:02-DPB1*68:01', 'HLA-DPA1*03:02-DPB1*69:01',
         'HLA-DPA1*03:02-DPB1*70:01', 'HLA-DPA1*03:02-DPB1*71:01', 'HLA-DPA1*03:02-DPB1*72:01', 'HLA-DPA1*03:02-DPB1*73:01',
         'HLA-DPA1*03:02-DPB1*74:01', 'HLA-DPA1*03:02-DPB1*75:01',
         'HLA-DPA1*03:02-DPB1*76:01', 'HLA-DPA1*03:02-DPB1*77:01', 'HLA-DPA1*03:02-DPB1*78:01', 'HLA-DPA1*03:02-DPB1*79:01',
         'HLA-DPA1*03:02-DPB1*80:01', 'HLA-DPA1*03:02-DPB1*81:01',
         'HLA-DPA1*03:02-DPB1*82:01', 'HLA-DPA1*03:02-DPB1*83:01', 'HLA-DPA1*03:02-DPB1*84:01', 'HLA-DPA1*03:02-DPB1*85:01',
         'HLA-DPA1*03:02-DPB1*86:01', 'HLA-DPA1*03:02-DPB1*87:01',
         'HLA-DPA1*03:02-DPB1*88:01', 'HLA-DPA1*03:02-DPB1*89:01', 'HLA-DPA1*03:02-DPB1*90:01', 'HLA-DPA1*03:02-DPB1*91:01',
         'HLA-DPA1*03:02-DPB1*92:01', 'HLA-DPA1*03:02-DPB1*93:01',
         'HLA-DPA1*03:02-DPB1*94:01', 'HLA-DPA1*03:02-DPB1*95:01', 'HLA-DPA1*03:02-DPB1*96:01', 'HLA-DPA1*03:02-DPB1*97:01',
         'HLA-DPA1*03:02-DPB1*98:01', 'HLA-DPA1*03:02-DPB1*99:01',
         'HLA-DPA1*03:03-DPB1*01:01', 'HLA-DPA1*03:03-DPB1*02:01', 'HLA-DPA1*03:03-DPB1*02:02', 'HLA-DPA1*03:03-DPB1*03:01',
         'HLA-DPA1*03:03-DPB1*04:01', 'HLA-DPA1*03:03-DPB1*04:02',
         'HLA-DPA1*03:03-DPB1*05:01', 'HLA-DPA1*03:03-DPB1*06:01', 'HLA-DPA1*03:03-DPB1*08:01', 'HLA-DPA1*03:03-DPB1*09:01',
         'HLA-DPA1*03:03-DPB1*10:001', 'HLA-DPA1*03:03-DPB1*10:01',
         'HLA-DPA1*03:03-DPB1*10:101', 'HLA-DPA1*03:03-DPB1*10:201', 'HLA-DPA1*03:03-DPB1*10:301', 'HLA-DPA1*03:03-DPB1*10:401',
         'HLA-DPA1*03:03-DPB1*10:501', 'HLA-DPA1*03:03-DPB1*10:601',
         'HLA-DPA1*03:03-DPB1*10:701', 'HLA-DPA1*03:03-DPB1*10:801', 'HLA-DPA1*03:03-DPB1*10:901', 'HLA-DPA1*03:03-DPB1*11:001',
         'HLA-DPA1*03:03-DPB1*11:01', 'HLA-DPA1*03:03-DPB1*11:101',
         'HLA-DPA1*03:03-DPB1*11:201', 'HLA-DPA1*03:03-DPB1*11:301', 'HLA-DPA1*03:03-DPB1*11:401', 'HLA-DPA1*03:03-DPB1*11:501',
         'HLA-DPA1*03:03-DPB1*11:601', 'HLA-DPA1*03:03-DPB1*11:701',
         'HLA-DPA1*03:03-DPB1*11:801', 'HLA-DPA1*03:03-DPB1*11:901', 'HLA-DPA1*03:03-DPB1*12:101', 'HLA-DPA1*03:03-DPB1*12:201',
         'HLA-DPA1*03:03-DPB1*12:301', 'HLA-DPA1*03:03-DPB1*12:401',
         'HLA-DPA1*03:03-DPB1*12:501', 'HLA-DPA1*03:03-DPB1*12:601', 'HLA-DPA1*03:03-DPB1*12:701', 'HLA-DPA1*03:03-DPB1*12:801',
         'HLA-DPA1*03:03-DPB1*12:901', 'HLA-DPA1*03:03-DPB1*13:001',
         'HLA-DPA1*03:03-DPB1*13:01', 'HLA-DPA1*03:03-DPB1*13:101', 'HLA-DPA1*03:03-DPB1*13:201', 'HLA-DPA1*03:03-DPB1*13:301',
         'HLA-DPA1*03:03-DPB1*13:401', 'HLA-DPA1*03:03-DPB1*14:01',
         'HLA-DPA1*03:03-DPB1*15:01', 'HLA-DPA1*03:03-DPB1*16:01', 'HLA-DPA1*03:03-DPB1*17:01', 'HLA-DPA1*03:03-DPB1*18:01',
         'HLA-DPA1*03:03-DPB1*19:01', 'HLA-DPA1*03:03-DPB1*20:01',
         'HLA-DPA1*03:03-DPB1*21:01', 'HLA-DPA1*03:03-DPB1*22:01', 'HLA-DPA1*03:03-DPB1*23:01', 'HLA-DPA1*03:03-DPB1*24:01',
         'HLA-DPA1*03:03-DPB1*25:01', 'HLA-DPA1*03:03-DPB1*26:01',
         'HLA-DPA1*03:03-DPB1*27:01', 'HLA-DPA1*03:03-DPB1*28:01', 'HLA-DPA1*03:03-DPB1*29:01', 'HLA-DPA1*03:03-DPB1*30:01',
         'HLA-DPA1*03:03-DPB1*31:01', 'HLA-DPA1*03:03-DPB1*32:01',
         'HLA-DPA1*03:03-DPB1*33:01', 'HLA-DPA1*03:03-DPB1*34:01', 'HLA-DPA1*03:03-DPB1*35:01', 'HLA-DPA1*03:03-DPB1*36:01',
         'HLA-DPA1*03:03-DPB1*37:01', 'HLA-DPA1*03:03-DPB1*38:01',
         'HLA-DPA1*03:03-DPB1*39:01', 'HLA-DPA1*03:03-DPB1*40:01', 'HLA-DPA1*03:03-DPB1*41:01', 'HLA-DPA1*03:03-DPB1*44:01',
         'HLA-DPA1*03:03-DPB1*45:01', 'HLA-DPA1*03:03-DPB1*46:01',
         'HLA-DPA1*03:03-DPB1*47:01', 'HLA-DPA1*03:03-DPB1*48:01', 'HLA-DPA1*03:03-DPB1*49:01', 'HLA-DPA1*03:03-DPB1*50:01',
         'HLA-DPA1*03:03-DPB1*51:01', 'HLA-DPA1*03:03-DPB1*52:01',
         'HLA-DPA1*03:03-DPB1*53:01', 'HLA-DPA1*03:03-DPB1*54:01', 'HLA-DPA1*03:03-DPB1*55:01', 'HLA-DPA1*03:03-DPB1*56:01',
         'HLA-DPA1*03:03-DPB1*58:01', 'HLA-DPA1*03:03-DPB1*59:01',
         'HLA-DPA1*03:03-DPB1*60:01', 'HLA-DPA1*03:03-DPB1*62:01', 'HLA-DPA1*03:03-DPB1*63:01', 'HLA-DPA1*03:03-DPB1*65:01',
         'HLA-DPA1*03:03-DPB1*66:01', 'HLA-DPA1*03:03-DPB1*67:01',
         'HLA-DPA1*03:03-DPB1*68:01', 'HLA-DPA1*03:03-DPB1*69:01', 'HLA-DPA1*03:03-DPB1*70:01', 'HLA-DPA1*03:03-DPB1*71:01',
         'HLA-DPA1*03:03-DPB1*72:01', 'HLA-DPA1*03:03-DPB1*73:01',
         'HLA-DPA1*03:03-DPB1*74:01', 'HLA-DPA1*03:03-DPB1*75:01', 'HLA-DPA1*03:03-DPB1*76:01', 'HLA-DPA1*03:03-DPB1*77:01',
         'HLA-DPA1*03:03-DPB1*78:01', 'HLA-DPA1*03:03-DPB1*79:01',
         'HLA-DPA1*03:03-DPB1*80:01', 'HLA-DPA1*03:03-DPB1*81:01', 'HLA-DPA1*03:03-DPB1*82:01', 'HLA-DPA1*03:03-DPB1*83:01',
         'HLA-DPA1*03:03-DPB1*84:01', 'HLA-DPA1*03:03-DPB1*85:01',
         'HLA-DPA1*03:03-DPB1*86:01', 'HLA-DPA1*03:03-DPB1*87:01', 'HLA-DPA1*03:03-DPB1*88:01', 'HLA-DPA1*03:03-DPB1*89:01',
         'HLA-DPA1*03:03-DPB1*90:01', 'HLA-DPA1*03:03-DPB1*91:01',
         'HLA-DPA1*03:03-DPB1*92:01', 'HLA-DPA1*03:03-DPB1*93:01', 'HLA-DPA1*03:03-DPB1*94:01', 'HLA-DPA1*03:03-DPB1*95:01',
         'HLA-DPA1*03:03-DPB1*96:01', 'HLA-DPA1*03:03-DPB1*97:01',
         'HLA-DPA1*03:03-DPB1*98:01', 'HLA-DPA1*03:03-DPB1*99:01', 'HLA-DPA1*04:01-DPB1*01:01', 'HLA-DPA1*04:01-DPB1*02:01',
         'HLA-DPA1*04:01-DPB1*02:02', 'HLA-DPA1*04:01-DPB1*03:01',
         'HLA-DPA1*04:01-DPB1*04:01', 'HLA-DPA1*04:01-DPB1*04:02', 'HLA-DPA1*04:01-DPB1*05:01', 'HLA-DPA1*04:01-DPB1*06:01',
         'HLA-DPA1*04:01-DPB1*08:01', 'HLA-DPA1*04:01-DPB1*09:01',
         'HLA-DPA1*04:01-DPB1*10:001', 'HLA-DPA1*04:01-DPB1*10:01', 'HLA-DPA1*04:01-DPB1*10:101', 'HLA-DPA1*04:01-DPB1*10:201',
         'HLA-DPA1*04:01-DPB1*10:301', 'HLA-DPA1*04:01-DPB1*10:401',
         'HLA-DPA1*04:01-DPB1*10:501', 'HLA-DPA1*04:01-DPB1*10:601', 'HLA-DPA1*04:01-DPB1*10:701', 'HLA-DPA1*04:01-DPB1*10:801',
         'HLA-DPA1*04:01-DPB1*10:901', 'HLA-DPA1*04:01-DPB1*11:001',
         'HLA-DPA1*04:01-DPB1*11:01', 'HLA-DPA1*04:01-DPB1*11:101', 'HLA-DPA1*04:01-DPB1*11:201', 'HLA-DPA1*04:01-DPB1*11:301',
         'HLA-DPA1*04:01-DPB1*11:401', 'HLA-DPA1*04:01-DPB1*11:501',
         'HLA-DPA1*04:01-DPB1*11:601', 'HLA-DPA1*04:01-DPB1*11:701', 'HLA-DPA1*04:01-DPB1*11:801', 'HLA-DPA1*04:01-DPB1*11:901',
         'HLA-DPA1*04:01-DPB1*12:101', 'HLA-DPA1*04:01-DPB1*12:201',
         'HLA-DPA1*04:01-DPB1*12:301', 'HLA-DPA1*04:01-DPB1*12:401', 'HLA-DPA1*04:01-DPB1*12:501', 'HLA-DPA1*04:01-DPB1*12:601',
         'HLA-DPA1*04:01-DPB1*12:701', 'HLA-DPA1*04:01-DPB1*12:801',
         'HLA-DPA1*04:01-DPB1*12:901', 'HLA-DPA1*04:01-DPB1*13:001', 'HLA-DPA1*04:01-DPB1*13:01', 'HLA-DPA1*04:01-DPB1*13:101',
         'HLA-DPA1*04:01-DPB1*13:201', 'HLA-DPA1*04:01-DPB1*13:301',
         'HLA-DPA1*04:01-DPB1*13:401', 'HLA-DPA1*04:01-DPB1*14:01', 'HLA-DPA1*04:01-DPB1*15:01', 'HLA-DPA1*04:01-DPB1*16:01',
         'HLA-DPA1*04:01-DPB1*17:01', 'HLA-DPA1*04:01-DPB1*18:01',
         'HLA-DPA1*04:01-DPB1*19:01', 'HLA-DPA1*04:01-DPB1*20:01', 'HLA-DPA1*04:01-DPB1*21:01', 'HLA-DPA1*04:01-DPB1*22:01',
         'HLA-DPA1*04:01-DPB1*23:01', 'HLA-DPA1*04:01-DPB1*24:01',
         'HLA-DPA1*04:01-DPB1*25:01', 'HLA-DPA1*04:01-DPB1*26:01', 'HLA-DPA1*04:01-DPB1*27:01', 'HLA-DPA1*04:01-DPB1*28:01',
         'HLA-DPA1*04:01-DPB1*29:01', 'HLA-DPA1*04:01-DPB1*30:01',
         'HLA-DPA1*04:01-DPB1*31:01', 'HLA-DPA1*04:01-DPB1*32:01', 'HLA-DPA1*04:01-DPB1*33:01', 'HLA-DPA1*04:01-DPB1*34:01',
         'HLA-DPA1*04:01-DPB1*35:01', 'HLA-DPA1*04:01-DPB1*36:01',
         'HLA-DPA1*04:01-DPB1*37:01', 'HLA-DPA1*04:01-DPB1*38:01', 'HLA-DPA1*04:01-DPB1*39:01', 'HLA-DPA1*04:01-DPB1*40:01',
         'HLA-DPA1*04:01-DPB1*41:01', 'HLA-DPA1*04:01-DPB1*44:01',
         'HLA-DPA1*04:01-DPB1*45:01', 'HLA-DPA1*04:01-DPB1*46:01', 'HLA-DPA1*04:01-DPB1*47:01', 'HLA-DPA1*04:01-DPB1*48:01',
         'HLA-DPA1*04:01-DPB1*49:01', 'HLA-DPA1*04:01-DPB1*50:01',
         'HLA-DPA1*04:01-DPB1*51:01', 'HLA-DPA1*04:01-DPB1*52:01', 'HLA-DPA1*04:01-DPB1*53:01', 'HLA-DPA1*04:01-DPB1*54:01',
         'HLA-DPA1*04:01-DPB1*55:01', 'HLA-DPA1*04:01-DPB1*56:01',
         'HLA-DPA1*04:01-DPB1*58:01', 'HLA-DPA1*04:01-DPB1*59:01', 'HLA-DPA1*04:01-DPB1*60:01', 'HLA-DPA1*04:01-DPB1*62:01',
         'HLA-DPA1*04:01-DPB1*63:01', 'HLA-DPA1*04:01-DPB1*65:01',
         'HLA-DPA1*04:01-DPB1*66:01', 'HLA-DPA1*04:01-DPB1*67:01', 'HLA-DPA1*04:01-DPB1*68:01', 'HLA-DPA1*04:01-DPB1*69:01',
         'HLA-DPA1*04:01-DPB1*70:01', 'HLA-DPA1*04:01-DPB1*71:01',
         'HLA-DPA1*04:01-DPB1*72:01', 'HLA-DPA1*04:01-DPB1*73:01', 'HLA-DPA1*04:01-DPB1*74:01', 'HLA-DPA1*04:01-DPB1*75:01',
         'HLA-DPA1*04:01-DPB1*76:01', 'HLA-DPA1*04:01-DPB1*77:01',
         'HLA-DPA1*04:01-DPB1*78:01', 'HLA-DPA1*04:01-DPB1*79:01', 'HLA-DPA1*04:01-DPB1*80:01', 'HLA-DPA1*04:01-DPB1*81:01',
         'HLA-DPA1*04:01-DPB1*82:01', 'HLA-DPA1*04:01-DPB1*83:01',
         'HLA-DPA1*04:01-DPB1*84:01', 'HLA-DPA1*04:01-DPB1*85:01', 'HLA-DPA1*04:01-DPB1*86:01', 'HLA-DPA1*04:01-DPB1*87:01',
         'HLA-DPA1*04:01-DPB1*88:01', 'HLA-DPA1*04:01-DPB1*89:01',
         'HLA-DPA1*04:01-DPB1*90:01', 'HLA-DPA1*04:01-DPB1*91:01', 'HLA-DPA1*04:01-DPB1*92:01', 'HLA-DPA1*04:01-DPB1*93:01',
         'HLA-DPA1*04:01-DPB1*94:01', 'HLA-DPA1*04:01-DPB1*95:01',
         'HLA-DPA1*04:01-DPB1*96:01', 'HLA-DPA1*04:01-DPB1*97:01', 'HLA-DPA1*04:01-DPB1*98:01', 'HLA-DPA1*04:01-DPB1*99:01',
         'HLA-DQA1*01:01-DQB1*02:01', 'HLA-DQA1*01:01-DQB1*02:02',
         'HLA-DQA1*01:01-DQB1*02:03', 'HLA-DQA1*01:01-DQB1*02:04', 'HLA-DQA1*01:01-DQB1*02:05', 'HLA-DQA1*01:01-DQB1*02:06',
         'HLA-DQA1*01:01-DQB1*03:01', 'HLA-DQA1*01:01-DQB1*03:02',
         'HLA-DQA1*01:01-DQB1*03:03', 'HLA-DQA1*01:01-DQB1*03:04', 'HLA-DQA1*01:01-DQB1*03:05', 'HLA-DQA1*01:01-DQB1*03:06',
         'HLA-DQA1*01:01-DQB1*03:07', 'HLA-DQA1*01:01-DQB1*03:08',
         'HLA-DQA1*01:01-DQB1*03:09', 'HLA-DQA1*01:01-DQB1*03:10', 'HLA-DQA1*01:01-DQB1*03:11', 'HLA-DQA1*01:01-DQB1*03:12',
         'HLA-DQA1*01:01-DQB1*03:13', 'HLA-DQA1*01:01-DQB1*03:14',
         'HLA-DQA1*01:01-DQB1*03:15', 'HLA-DQA1*01:01-DQB1*03:16', 'HLA-DQA1*01:01-DQB1*03:17', 'HLA-DQA1*01:01-DQB1*03:18',
         'HLA-DQA1*01:01-DQB1*03:19', 'HLA-DQA1*01:01-DQB1*03:20',
         'HLA-DQA1*01:01-DQB1*03:21', 'HLA-DQA1*01:01-DQB1*03:22', 'HLA-DQA1*01:01-DQB1*03:23', 'HLA-DQA1*01:01-DQB1*03:24',
         'HLA-DQA1*01:01-DQB1*03:25', 'HLA-DQA1*01:01-DQB1*03:26',
         'HLA-DQA1*01:01-DQB1*03:27', 'HLA-DQA1*01:01-DQB1*03:28', 'HLA-DQA1*01:01-DQB1*03:29', 'HLA-DQA1*01:01-DQB1*03:30',
         'HLA-DQA1*01:01-DQB1*03:31', 'HLA-DQA1*01:01-DQB1*03:32',
         'HLA-DQA1*01:01-DQB1*03:33', 'HLA-DQA1*01:01-DQB1*03:34', 'HLA-DQA1*01:01-DQB1*03:35', 'HLA-DQA1*01:01-DQB1*03:36',
         'HLA-DQA1*01:01-DQB1*03:37', 'HLA-DQA1*01:01-DQB1*03:38',
         'HLA-DQA1*01:01-DQB1*04:01', 'HLA-DQA1*01:01-DQB1*04:02', 'HLA-DQA1*01:01-DQB1*04:03', 'HLA-DQA1*01:01-DQB1*04:04',
         'HLA-DQA1*01:01-DQB1*04:05', 'HLA-DQA1*01:01-DQB1*04:06',
         'HLA-DQA1*01:01-DQB1*04:07', 'HLA-DQA1*01:01-DQB1*04:08', 'HLA-DQA1*01:01-DQB1*05:01', 'HLA-DQA1*01:01-DQB1*05:02',
         'HLA-DQA1*01:01-DQB1*05:03', 'HLA-DQA1*01:01-DQB1*05:05',
         'HLA-DQA1*01:01-DQB1*05:06', 'HLA-DQA1*01:01-DQB1*05:07', 'HLA-DQA1*01:01-DQB1*05:08', 'HLA-DQA1*01:01-DQB1*05:09',
         'HLA-DQA1*01:01-DQB1*05:10', 'HLA-DQA1*01:01-DQB1*05:11',
         'HLA-DQA1*01:01-DQB1*05:12', 'HLA-DQA1*01:01-DQB1*05:13', 'HLA-DQA1*01:01-DQB1*05:14', 'HLA-DQA1*01:01-DQB1*06:01',
         'HLA-DQA1*01:01-DQB1*06:02', 'HLA-DQA1*01:01-DQB1*06:03',
         'HLA-DQA1*01:01-DQB1*06:04', 'HLA-DQA1*01:01-DQB1*06:07', 'HLA-DQA1*01:01-DQB1*06:08', 'HLA-DQA1*01:01-DQB1*06:09',
         'HLA-DQA1*01:01-DQB1*06:10', 'HLA-DQA1*01:01-DQB1*06:11',
         'HLA-DQA1*01:01-DQB1*06:12', 'HLA-DQA1*01:01-DQB1*06:14', 'HLA-DQA1*01:01-DQB1*06:15', 'HLA-DQA1*01:01-DQB1*06:16',
         'HLA-DQA1*01:01-DQB1*06:17', 'HLA-DQA1*01:01-DQB1*06:18',
         'HLA-DQA1*01:01-DQB1*06:19', 'HLA-DQA1*01:01-DQB1*06:21', 'HLA-DQA1*01:01-DQB1*06:22', 'HLA-DQA1*01:01-DQB1*06:23',
         'HLA-DQA1*01:01-DQB1*06:24', 'HLA-DQA1*01:01-DQB1*06:25',
         'HLA-DQA1*01:01-DQB1*06:27', 'HLA-DQA1*01:01-DQB1*06:28', 'HLA-DQA1*01:01-DQB1*06:29', 'HLA-DQA1*01:01-DQB1*06:30',
         'HLA-DQA1*01:01-DQB1*06:31', 'HLA-DQA1*01:01-DQB1*06:32',
         'HLA-DQA1*01:01-DQB1*06:33', 'HLA-DQA1*01:01-DQB1*06:34', 'HLA-DQA1*01:01-DQB1*06:35', 'HLA-DQA1*01:01-DQB1*06:36',
         'HLA-DQA1*01:01-DQB1*06:37', 'HLA-DQA1*01:01-DQB1*06:38',
         'HLA-DQA1*01:01-DQB1*06:39', 'HLA-DQA1*01:01-DQB1*06:40', 'HLA-DQA1*01:01-DQB1*06:41', 'HLA-DQA1*01:01-DQB1*06:42',
         'HLA-DQA1*01:01-DQB1*06:43', 'HLA-DQA1*01:01-DQB1*06:44',
         'HLA-DQA1*01:02-DQB1*02:01', 'HLA-DQA1*01:02-DQB1*02:02', 'HLA-DQA1*01:02-DQB1*02:03', 'HLA-DQA1*01:02-DQB1*02:04',
         'HLA-DQA1*01:02-DQB1*02:05', 'HLA-DQA1*01:02-DQB1*02:06',
         'HLA-DQA1*01:02-DQB1*03:01', 'HLA-DQA1*01:02-DQB1*03:02', 'HLA-DQA1*01:02-DQB1*03:03', 'HLA-DQA1*01:02-DQB1*03:04',
         'HLA-DQA1*01:02-DQB1*03:05', 'HLA-DQA1*01:02-DQB1*03:06',
         'HLA-DQA1*01:02-DQB1*03:07', 'HLA-DQA1*01:02-DQB1*03:08', 'HLA-DQA1*01:02-DQB1*03:09', 'HLA-DQA1*01:02-DQB1*03:10',
         'HLA-DQA1*01:02-DQB1*03:11', 'HLA-DQA1*01:02-DQB1*03:12',
         'HLA-DQA1*01:02-DQB1*03:13', 'HLA-DQA1*01:02-DQB1*03:14', 'HLA-DQA1*01:02-DQB1*03:15', 'HLA-DQA1*01:02-DQB1*03:16',
         'HLA-DQA1*01:02-DQB1*03:17', 'HLA-DQA1*01:02-DQB1*03:18',
         'HLA-DQA1*01:02-DQB1*03:19', 'HLA-DQA1*01:02-DQB1*03:20', 'HLA-DQA1*01:02-DQB1*03:21', 'HLA-DQA1*01:02-DQB1*03:22',
         'HLA-DQA1*01:02-DQB1*03:23', 'HLA-DQA1*01:02-DQB1*03:24',
         'HLA-DQA1*01:02-DQB1*03:25', 'HLA-DQA1*01:02-DQB1*03:26', 'HLA-DQA1*01:02-DQB1*03:27', 'HLA-DQA1*01:02-DQB1*03:28',
         'HLA-DQA1*01:02-DQB1*03:29', 'HLA-DQA1*01:02-DQB1*03:30',
         'HLA-DQA1*01:02-DQB1*03:31', 'HLA-DQA1*01:02-DQB1*03:32', 'HLA-DQA1*01:02-DQB1*03:33', 'HLA-DQA1*01:02-DQB1*03:34',
         'HLA-DQA1*01:02-DQB1*03:35', 'HLA-DQA1*01:02-DQB1*03:36',
         'HLA-DQA1*01:02-DQB1*03:37', 'HLA-DQA1*01:02-DQB1*03:38', 'HLA-DQA1*01:02-DQB1*04:01', 'HLA-DQA1*01:02-DQB1*04:02',
         'HLA-DQA1*01:02-DQB1*04:03', 'HLA-DQA1*01:02-DQB1*04:04',
         'HLA-DQA1*01:02-DQB1*04:05', 'HLA-DQA1*01:02-DQB1*04:06', 'HLA-DQA1*01:02-DQB1*04:07', 'HLA-DQA1*01:02-DQB1*04:08',
         'HLA-DQA1*01:02-DQB1*05:01', 'HLA-DQA1*01:02-DQB1*05:02',
         'HLA-DQA1*01:02-DQB1*05:03', 'HLA-DQA1*01:02-DQB1*05:05', 'HLA-DQA1*01:02-DQB1*05:06', 'HLA-DQA1*01:02-DQB1*05:07',
         'HLA-DQA1*01:02-DQB1*05:08', 'HLA-DQA1*01:02-DQB1*05:09',
         'HLA-DQA1*01:02-DQB1*05:10', 'HLA-DQA1*01:02-DQB1*05:11', 'HLA-DQA1*01:02-DQB1*05:12', 'HLA-DQA1*01:02-DQB1*05:13',
         'HLA-DQA1*01:02-DQB1*05:14', 'HLA-DQA1*01:02-DQB1*06:01',
         'HLA-DQA1*01:02-DQB1*06:02', 'HLA-DQA1*01:02-DQB1*06:03', 'HLA-DQA1*01:02-DQB1*06:04', 'HLA-DQA1*01:02-DQB1*06:07',
         'HLA-DQA1*01:02-DQB1*06:08', 'HLA-DQA1*01:02-DQB1*06:09',
         'HLA-DQA1*01:02-DQB1*06:10', 'HLA-DQA1*01:02-DQB1*06:11', 'HLA-DQA1*01:02-DQB1*06:12', 'HLA-DQA1*01:02-DQB1*06:14',
         'HLA-DQA1*01:02-DQB1*06:15', 'HLA-DQA1*01:02-DQB1*06:16',
         'HLA-DQA1*01:02-DQB1*06:17', 'HLA-DQA1*01:02-DQB1*06:18', 'HLA-DQA1*01:02-DQB1*06:19', 'HLA-DQA1*01:02-DQB1*06:21',
         'HLA-DQA1*01:02-DQB1*06:22', 'HLA-DQA1*01:02-DQB1*06:23',
         'HLA-DQA1*01:02-DQB1*06:24', 'HLA-DQA1*01:02-DQB1*06:25', 'HLA-DQA1*01:02-DQB1*06:27', 'HLA-DQA1*01:02-DQB1*06:28',
         'HLA-DQA1*01:02-DQB1*06:29', 'HLA-DQA1*01:02-DQB1*06:30',
         'HLA-DQA1*01:02-DQB1*06:31', 'HLA-DQA1*01:02-DQB1*06:32', 'HLA-DQA1*01:02-DQB1*06:33', 'HLA-DQA1*01:02-DQB1*06:34',
         'HLA-DQA1*01:02-DQB1*06:35', 'HLA-DQA1*01:02-DQB1*06:36',
         'HLA-DQA1*01:02-DQB1*06:37', 'HLA-DQA1*01:02-DQB1*06:38', 'HLA-DQA1*01:02-DQB1*06:39', 'HLA-DQA1*01:02-DQB1*06:40',
         'HLA-DQA1*01:02-DQB1*06:41', 'HLA-DQA1*01:02-DQB1*06:42',
         'HLA-DQA1*01:02-DQB1*06:43', 'HLA-DQA1*01:02-DQB1*06:44', 'HLA-DQA1*01:03-DQB1*02:01', 'HLA-DQA1*01:03-DQB1*02:02',
         'HLA-DQA1*01:03-DQB1*02:03', 'HLA-DQA1*01:03-DQB1*02:04',
         'HLA-DQA1*01:03-DQB1*02:05', 'HLA-DQA1*01:03-DQB1*02:06', 'HLA-DQA1*01:03-DQB1*03:01', 'HLA-DQA1*01:03-DQB1*03:02',
         'HLA-DQA1*01:03-DQB1*03:03', 'HLA-DQA1*01:03-DQB1*03:04',
         'HLA-DQA1*01:03-DQB1*03:05', 'HLA-DQA1*01:03-DQB1*03:06', 'HLA-DQA1*01:03-DQB1*03:07', 'HLA-DQA1*01:03-DQB1*03:08',
         'HLA-DQA1*01:03-DQB1*03:09', 'HLA-DQA1*01:03-DQB1*03:10',
         'HLA-DQA1*01:03-DQB1*03:11', 'HLA-DQA1*01:03-DQB1*03:12', 'HLA-DQA1*01:03-DQB1*03:13', 'HLA-DQA1*01:03-DQB1*03:14',
         'HLA-DQA1*01:03-DQB1*03:15', 'HLA-DQA1*01:03-DQB1*03:16',
         'HLA-DQA1*01:03-DQB1*03:17', 'HLA-DQA1*01:03-DQB1*03:18', 'HLA-DQA1*01:03-DQB1*03:19', 'HLA-DQA1*01:03-DQB1*03:20',
         'HLA-DQA1*01:03-DQB1*03:21', 'HLA-DQA1*01:03-DQB1*03:22',
         'HLA-DQA1*01:03-DQB1*03:23', 'HLA-DQA1*01:03-DQB1*03:24', 'HLA-DQA1*01:03-DQB1*03:25', 'HLA-DQA1*01:03-DQB1*03:26',
         'HLA-DQA1*01:03-DQB1*03:27', 'HLA-DQA1*01:03-DQB1*03:28',
         'HLA-DQA1*01:03-DQB1*03:29', 'HLA-DQA1*01:03-DQB1*03:30', 'HLA-DQA1*01:03-DQB1*03:31', 'HLA-DQA1*01:03-DQB1*03:32',
         'HLA-DQA1*01:03-DQB1*03:33', 'HLA-DQA1*01:03-DQB1*03:34',
         'HLA-DQA1*01:03-DQB1*03:35', 'HLA-DQA1*01:03-DQB1*03:36', 'HLA-DQA1*01:03-DQB1*03:37', 'HLA-DQA1*01:03-DQB1*03:38',
         'HLA-DQA1*01:03-DQB1*04:01', 'HLA-DQA1*01:03-DQB1*04:02',
         'HLA-DQA1*01:03-DQB1*04:03', 'HLA-DQA1*01:03-DQB1*04:04', 'HLA-DQA1*01:03-DQB1*04:05', 'HLA-DQA1*01:03-DQB1*04:06',
         'HLA-DQA1*01:03-DQB1*04:07', 'HLA-DQA1*01:03-DQB1*04:08',
         'HLA-DQA1*01:03-DQB1*05:01', 'HLA-DQA1*01:03-DQB1*05:02', 'HLA-DQA1*01:03-DQB1*05:03', 'HLA-DQA1*01:03-DQB1*05:05',
         'HLA-DQA1*01:03-DQB1*05:06', 'HLA-DQA1*01:03-DQB1*05:07',
         'HLA-DQA1*01:03-DQB1*05:08', 'HLA-DQA1*01:03-DQB1*05:09', 'HLA-DQA1*01:03-DQB1*05:10', 'HLA-DQA1*01:03-DQB1*05:11',
         'HLA-DQA1*01:03-DQB1*05:12', 'HLA-DQA1*01:03-DQB1*05:13',
         'HLA-DQA1*01:03-DQB1*05:14', 'HLA-DQA1*01:03-DQB1*06:01', 'HLA-DQA1*01:03-DQB1*06:02', 'HLA-DQA1*01:03-DQB1*06:03',
         'HLA-DQA1*01:03-DQB1*06:04', 'HLA-DQA1*01:03-DQB1*06:07',
         'HLA-DQA1*01:03-DQB1*06:08', 'HLA-DQA1*01:03-DQB1*06:09', 'HLA-DQA1*01:03-DQB1*06:10', 'HLA-DQA1*01:03-DQB1*06:11',
         'HLA-DQA1*01:03-DQB1*06:12', 'HLA-DQA1*01:03-DQB1*06:14',
         'HLA-DQA1*01:03-DQB1*06:15', 'HLA-DQA1*01:03-DQB1*06:16', 'HLA-DQA1*01:03-DQB1*06:17', 'HLA-DQA1*01:03-DQB1*06:18',
         'HLA-DQA1*01:03-DQB1*06:19', 'HLA-DQA1*01:03-DQB1*06:21',
         'HLA-DQA1*01:03-DQB1*06:22', 'HLA-DQA1*01:03-DQB1*06:23', 'HLA-DQA1*01:03-DQB1*06:24', 'HLA-DQA1*01:03-DQB1*06:25',
         'HLA-DQA1*01:03-DQB1*06:27', 'HLA-DQA1*01:03-DQB1*06:28',
         'HLA-DQA1*01:03-DQB1*06:29', 'HLA-DQA1*01:03-DQB1*06:30', 'HLA-DQA1*01:03-DQB1*06:31', 'HLA-DQA1*01:03-DQB1*06:32',
         'HLA-DQA1*01:03-DQB1*06:33', 'HLA-DQA1*01:03-DQB1*06:34',
         'HLA-DQA1*01:03-DQB1*06:35', 'HLA-DQA1*01:03-DQB1*06:36', 'HLA-DQA1*01:03-DQB1*06:37', 'HLA-DQA1*01:03-DQB1*06:38',
         'HLA-DQA1*01:03-DQB1*06:39', 'HLA-DQA1*01:03-DQB1*06:40',
         'HLA-DQA1*01:03-DQB1*06:41', 'HLA-DQA1*01:03-DQB1*06:42', 'HLA-DQA1*01:03-DQB1*06:43', 'HLA-DQA1*01:03-DQB1*06:44',
         'HLA-DQA1*01:04-DQB1*02:01', 'HLA-DQA1*01:04-DQB1*02:02',
         'HLA-DQA1*01:04-DQB1*02:03', 'HLA-DQA1*01:04-DQB1*02:04', 'HLA-DQA1*01:04-DQB1*02:05', 'HLA-DQA1*01:04-DQB1*02:06',
         'HLA-DQA1*01:04-DQB1*03:01', 'HLA-DQA1*01:04-DQB1*03:02',
         'HLA-DQA1*01:04-DQB1*03:03', 'HLA-DQA1*01:04-DQB1*03:04', 'HLA-DQA1*01:04-DQB1*03:05', 'HLA-DQA1*01:04-DQB1*03:06',
         'HLA-DQA1*01:04-DQB1*03:07', 'HLA-DQA1*01:04-DQB1*03:08',
         'HLA-DQA1*01:04-DQB1*03:09', 'HLA-DQA1*01:04-DQB1*03:10', 'HLA-DQA1*01:04-DQB1*03:11', 'HLA-DQA1*01:04-DQB1*03:12',
         'HLA-DQA1*01:04-DQB1*03:13', 'HLA-DQA1*01:04-DQB1*03:14',
         'HLA-DQA1*01:04-DQB1*03:15', 'HLA-DQA1*01:04-DQB1*03:16', 'HLA-DQA1*01:04-DQB1*03:17', 'HLA-DQA1*01:04-DQB1*03:18',
         'HLA-DQA1*01:04-DQB1*03:19', 'HLA-DQA1*01:04-DQB1*03:20',
         'HLA-DQA1*01:04-DQB1*03:21', 'HLA-DQA1*01:04-DQB1*03:22', 'HLA-DQA1*01:04-DQB1*03:23', 'HLA-DQA1*01:04-DQB1*03:24',
         'HLA-DQA1*01:04-DQB1*03:25', 'HLA-DQA1*01:04-DQB1*03:26',
         'HLA-DQA1*01:04-DQB1*03:27', 'HLA-DQA1*01:04-DQB1*03:28', 'HLA-DQA1*01:04-DQB1*03:29', 'HLA-DQA1*01:04-DQB1*03:30',
         'HLA-DQA1*01:04-DQB1*03:31', 'HLA-DQA1*01:04-DQB1*03:32',
         'HLA-DQA1*01:04-DQB1*03:33', 'HLA-DQA1*01:04-DQB1*03:34', 'HLA-DQA1*01:04-DQB1*03:35', 'HLA-DQA1*01:04-DQB1*03:36',
         'HLA-DQA1*01:04-DQB1*03:37', 'HLA-DQA1*01:04-DQB1*03:38',
         'HLA-DQA1*01:04-DQB1*04:01', 'HLA-DQA1*01:04-DQB1*04:02', 'HLA-DQA1*01:04-DQB1*04:03', 'HLA-DQA1*01:04-DQB1*04:04',
         'HLA-DQA1*01:04-DQB1*04:05', 'HLA-DQA1*01:04-DQB1*04:06',
         'HLA-DQA1*01:04-DQB1*04:07', 'HLA-DQA1*01:04-DQB1*04:08', 'HLA-DQA1*01:04-DQB1*05:01', 'HLA-DQA1*01:04-DQB1*05:02',
         'HLA-DQA1*01:04-DQB1*05:03', 'HLA-DQA1*01:04-DQB1*05:05',
         'HLA-DQA1*01:04-DQB1*05:06', 'HLA-DQA1*01:04-DQB1*05:07', 'HLA-DQA1*01:04-DQB1*05:08', 'HLA-DQA1*01:04-DQB1*05:09',
         'HLA-DQA1*01:04-DQB1*05:10', 'HLA-DQA1*01:04-DQB1*05:11',
         'HLA-DQA1*01:04-DQB1*05:12', 'HLA-DQA1*01:04-DQB1*05:13', 'HLA-DQA1*01:04-DQB1*05:14', 'HLA-DQA1*01:04-DQB1*06:01',
         'HLA-DQA1*01:04-DQB1*06:02', 'HLA-DQA1*01:04-DQB1*06:03',
         'HLA-DQA1*01:04-DQB1*06:04', 'HLA-DQA1*01:04-DQB1*06:07', 'HLA-DQA1*01:04-DQB1*06:08', 'HLA-DQA1*01:04-DQB1*06:09',
         'HLA-DQA1*01:04-DQB1*06:10', 'HLA-DQA1*01:04-DQB1*06:11',
         'HLA-DQA1*01:04-DQB1*06:12', 'HLA-DQA1*01:04-DQB1*06:14', 'HLA-DQA1*01:04-DQB1*06:15', 'HLA-DQA1*01:04-DQB1*06:16',
         'HLA-DQA1*01:04-DQB1*06:17', 'HLA-DQA1*01:04-DQB1*06:18',
         'HLA-DQA1*01:04-DQB1*06:19', 'HLA-DQA1*01:04-DQB1*06:21', 'HLA-DQA1*01:04-DQB1*06:22', 'HLA-DQA1*01:04-DQB1*06:23',
         'HLA-DQA1*01:04-DQB1*06:24', 'HLA-DQA1*01:04-DQB1*06:25',
         'HLA-DQA1*01:04-DQB1*06:27', 'HLA-DQA1*01:04-DQB1*06:28', 'HLA-DQA1*01:04-DQB1*06:29', 'HLA-DQA1*01:04-DQB1*06:30',
         'HLA-DQA1*01:04-DQB1*06:31', 'HLA-DQA1*01:04-DQB1*06:32',
         'HLA-DQA1*01:04-DQB1*06:33', 'HLA-DQA1*01:04-DQB1*06:34', 'HLA-DQA1*01:04-DQB1*06:35', 'HLA-DQA1*01:04-DQB1*06:36',
         'HLA-DQA1*01:04-DQB1*06:37', 'HLA-DQA1*01:04-DQB1*06:38',
         'HLA-DQA1*01:04-DQB1*06:39', 'HLA-DQA1*01:04-DQB1*06:40', 'HLA-DQA1*01:04-DQB1*06:41', 'HLA-DQA1*01:04-DQB1*06:42',
         'HLA-DQA1*01:04-DQB1*06:43', 'HLA-DQA1*01:04-DQB1*06:44',
         'HLA-DQA1*01:05-DQB1*02:01', 'HLA-DQA1*01:05-DQB1*02:02', 'HLA-DQA1*01:05-DQB1*02:03', 'HLA-DQA1*01:05-DQB1*02:04',
         'HLA-DQA1*01:05-DQB1*02:05', 'HLA-DQA1*01:05-DQB1*02:06',
         'HLA-DQA1*01:05-DQB1*03:01', 'HLA-DQA1*01:05-DQB1*03:02', 'HLA-DQA1*01:05-DQB1*03:03', 'HLA-DQA1*01:05-DQB1*03:04',
         'HLA-DQA1*01:05-DQB1*03:05', 'HLA-DQA1*01:05-DQB1*03:06',
         'HLA-DQA1*01:05-DQB1*03:07', 'HLA-DQA1*01:05-DQB1*03:08', 'HLA-DQA1*01:05-DQB1*03:09', 'HLA-DQA1*01:05-DQB1*03:10',
         'HLA-DQA1*01:05-DQB1*03:11', 'HLA-DQA1*01:05-DQB1*03:12',
         'HLA-DQA1*01:05-DQB1*03:13', 'HLA-DQA1*01:05-DQB1*03:14', 'HLA-DQA1*01:05-DQB1*03:15', 'HLA-DQA1*01:05-DQB1*03:16',
         'HLA-DQA1*01:05-DQB1*03:17', 'HLA-DQA1*01:05-DQB1*03:18',
         'HLA-DQA1*01:05-DQB1*03:19', 'HLA-DQA1*01:05-DQB1*03:20', 'HLA-DQA1*01:05-DQB1*03:21', 'HLA-DQA1*01:05-DQB1*03:22',
         'HLA-DQA1*01:05-DQB1*03:23', 'HLA-DQA1*01:05-DQB1*03:24',
         'HLA-DQA1*01:05-DQB1*03:25', 'HLA-DQA1*01:05-DQB1*03:26', 'HLA-DQA1*01:05-DQB1*03:27', 'HLA-DQA1*01:05-DQB1*03:28',
         'HLA-DQA1*01:05-DQB1*03:29', 'HLA-DQA1*01:05-DQB1*03:30',
         'HLA-DQA1*01:05-DQB1*03:31', 'HLA-DQA1*01:05-DQB1*03:32', 'HLA-DQA1*01:05-DQB1*03:33', 'HLA-DQA1*01:05-DQB1*03:34',
         'HLA-DQA1*01:05-DQB1*03:35', 'HLA-DQA1*01:05-DQB1*03:36',
         'HLA-DQA1*01:05-DQB1*03:37', 'HLA-DQA1*01:05-DQB1*03:38', 'HLA-DQA1*01:05-DQB1*04:01', 'HLA-DQA1*01:05-DQB1*04:02',
         'HLA-DQA1*01:05-DQB1*04:03', 'HLA-DQA1*01:05-DQB1*04:04',
         'HLA-DQA1*01:05-DQB1*04:05', 'HLA-DQA1*01:05-DQB1*04:06', 'HLA-DQA1*01:05-DQB1*04:07', 'HLA-DQA1*01:05-DQB1*04:08',
         'HLA-DQA1*01:05-DQB1*05:01', 'HLA-DQA1*01:05-DQB1*05:02',
         'HLA-DQA1*01:05-DQB1*05:03', 'HLA-DQA1*01:05-DQB1*05:05', 'HLA-DQA1*01:05-DQB1*05:06', 'HLA-DQA1*01:05-DQB1*05:07',
         'HLA-DQA1*01:05-DQB1*05:08', 'HLA-DQA1*01:05-DQB1*05:09',
         'HLA-DQA1*01:05-DQB1*05:10', 'HLA-DQA1*01:05-DQB1*05:11', 'HLA-DQA1*01:05-DQB1*05:12', 'HLA-DQA1*01:05-DQB1*05:13',
         'HLA-DQA1*01:05-DQB1*05:14', 'HLA-DQA1*01:05-DQB1*06:01',
         'HLA-DQA1*01:05-DQB1*06:02', 'HLA-DQA1*01:05-DQB1*06:03', 'HLA-DQA1*01:05-DQB1*06:04', 'HLA-DQA1*01:05-DQB1*06:07',
         'HLA-DQA1*01:05-DQB1*06:08', 'HLA-DQA1*01:05-DQB1*06:09',
         'HLA-DQA1*01:05-DQB1*06:10', 'HLA-DQA1*01:05-DQB1*06:11', 'HLA-DQA1*01:05-DQB1*06:12', 'HLA-DQA1*01:05-DQB1*06:14',
         'HLA-DQA1*01:05-DQB1*06:15', 'HLA-DQA1*01:05-DQB1*06:16',
         'HLA-DQA1*01:05-DQB1*06:17', 'HLA-DQA1*01:05-DQB1*06:18', 'HLA-DQA1*01:05-DQB1*06:19', 'HLA-DQA1*01:05-DQB1*06:21',
         'HLA-DQA1*01:05-DQB1*06:22', 'HLA-DQA1*01:05-DQB1*06:23',
         'HLA-DQA1*01:05-DQB1*06:24', 'HLA-DQA1*01:05-DQB1*06:25', 'HLA-DQA1*01:05-DQB1*06:27', 'HLA-DQA1*01:05-DQB1*06:28',
         'HLA-DQA1*01:05-DQB1*06:29', 'HLA-DQA1*01:05-DQB1*06:30',
         'HLA-DQA1*01:05-DQB1*06:31', 'HLA-DQA1*01:05-DQB1*06:32', 'HLA-DQA1*01:05-DQB1*06:33', 'HLA-DQA1*01:05-DQB1*06:34',
         'HLA-DQA1*01:05-DQB1*06:35', 'HLA-DQA1*01:05-DQB1*06:36',
         'HLA-DQA1*01:05-DQB1*06:37', 'HLA-DQA1*01:05-DQB1*06:38', 'HLA-DQA1*01:05-DQB1*06:39', 'HLA-DQA1*01:05-DQB1*06:40',
         'HLA-DQA1*01:05-DQB1*06:41', 'HLA-DQA1*01:05-DQB1*06:42',
         'HLA-DQA1*01:05-DQB1*06:43', 'HLA-DQA1*01:05-DQB1*06:44', 'HLA-DQA1*01:06-DQB1*02:01', 'HLA-DQA1*01:06-DQB1*02:02',
         'HLA-DQA1*01:06-DQB1*02:03', 'HLA-DQA1*01:06-DQB1*02:04',
         'HLA-DQA1*01:06-DQB1*02:05', 'HLA-DQA1*01:06-DQB1*02:06', 'HLA-DQA1*01:06-DQB1*03:01', 'HLA-DQA1*01:06-DQB1*03:02',
         'HLA-DQA1*01:06-DQB1*03:03', 'HLA-DQA1*01:06-DQB1*03:04',
         'HLA-DQA1*01:06-DQB1*03:05', 'HLA-DQA1*01:06-DQB1*03:06', 'HLA-DQA1*01:06-DQB1*03:07', 'HLA-DQA1*01:06-DQB1*03:08',
         'HLA-DQA1*01:06-DQB1*03:09', 'HLA-DQA1*01:06-DQB1*03:10',
         'HLA-DQA1*01:06-DQB1*03:11', 'HLA-DQA1*01:06-DQB1*03:12', 'HLA-DQA1*01:06-DQB1*03:13', 'HLA-DQA1*01:06-DQB1*03:14',
         'HLA-DQA1*01:06-DQB1*03:15', 'HLA-DQA1*01:06-DQB1*03:16',
         'HLA-DQA1*01:06-DQB1*03:17', 'HLA-DQA1*01:06-DQB1*03:18', 'HLA-DQA1*01:06-DQB1*03:19', 'HLA-DQA1*01:06-DQB1*03:20',
         'HLA-DQA1*01:06-DQB1*03:21', 'HLA-DQA1*01:06-DQB1*03:22',
         'HLA-DQA1*01:06-DQB1*03:23', 'HLA-DQA1*01:06-DQB1*03:24', 'HLA-DQA1*01:06-DQB1*03:25', 'HLA-DQA1*01:06-DQB1*03:26',
         'HLA-DQA1*01:06-DQB1*03:27', 'HLA-DQA1*01:06-DQB1*03:28',
         'HLA-DQA1*01:06-DQB1*03:29', 'HLA-DQA1*01:06-DQB1*03:30', 'HLA-DQA1*01:06-DQB1*03:31', 'HLA-DQA1*01:06-DQB1*03:32',
         'HLA-DQA1*01:06-DQB1*03:33', 'HLA-DQA1*01:06-DQB1*03:34',
         'HLA-DQA1*01:06-DQB1*03:35', 'HLA-DQA1*01:06-DQB1*03:36', 'HLA-DQA1*01:06-DQB1*03:37', 'HLA-DQA1*01:06-DQB1*03:38',
         'HLA-DQA1*01:06-DQB1*04:01', 'HLA-DQA1*01:06-DQB1*04:02',
         'HLA-DQA1*01:06-DQB1*04:03', 'HLA-DQA1*01:06-DQB1*04:04', 'HLA-DQA1*01:06-DQB1*04:05', 'HLA-DQA1*01:06-DQB1*04:06',
         'HLA-DQA1*01:06-DQB1*04:07', 'HLA-DQA1*01:06-DQB1*04:08',
         'HLA-DQA1*01:06-DQB1*05:01', 'HLA-DQA1*01:06-DQB1*05:02', 'HLA-DQA1*01:06-DQB1*05:03', 'HLA-DQA1*01:06-DQB1*05:05',
         'HLA-DQA1*01:06-DQB1*05:06', 'HLA-DQA1*01:06-DQB1*05:07',
         'HLA-DQA1*01:06-DQB1*05:08', 'HLA-DQA1*01:06-DQB1*05:09', 'HLA-DQA1*01:06-DQB1*05:10', 'HLA-DQA1*01:06-DQB1*05:11',
         'HLA-DQA1*01:06-DQB1*05:12', 'HLA-DQA1*01:06-DQB1*05:13',
         'HLA-DQA1*01:06-DQB1*05:14', 'HLA-DQA1*01:06-DQB1*06:01', 'HLA-DQA1*01:06-DQB1*06:02', 'HLA-DQA1*01:06-DQB1*06:03',
         'HLA-DQA1*01:06-DQB1*06:04', 'HLA-DQA1*01:06-DQB1*06:07',
         'HLA-DQA1*01:06-DQB1*06:08', 'HLA-DQA1*01:06-DQB1*06:09', 'HLA-DQA1*01:06-DQB1*06:10', 'HLA-DQA1*01:06-DQB1*06:11',
         'HLA-DQA1*01:06-DQB1*06:12', 'HLA-DQA1*01:06-DQB1*06:14',
         'HLA-DQA1*01:06-DQB1*06:15', 'HLA-DQA1*01:06-DQB1*06:16', 'HLA-DQA1*01:06-DQB1*06:17', 'HLA-DQA1*01:06-DQB1*06:18',
         'HLA-DQA1*01:06-DQB1*06:19', 'HLA-DQA1*01:06-DQB1*06:21',
         'HLA-DQA1*01:06-DQB1*06:22', 'HLA-DQA1*01:06-DQB1*06:23', 'HLA-DQA1*01:06-DQB1*06:24', 'HLA-DQA1*01:06-DQB1*06:25',
         'HLA-DQA1*01:06-DQB1*06:27', 'HLA-DQA1*01:06-DQB1*06:28',
         'HLA-DQA1*01:06-DQB1*06:29', 'HLA-DQA1*01:06-DQB1*06:30', 'HLA-DQA1*01:06-DQB1*06:31', 'HLA-DQA1*01:06-DQB1*06:32',
         'HLA-DQA1*01:06-DQB1*06:33', 'HLA-DQA1*01:06-DQB1*06:34',
         'HLA-DQA1*01:06-DQB1*06:35', 'HLA-DQA1*01:06-DQB1*06:36', 'HLA-DQA1*01:06-DQB1*06:37', 'HLA-DQA1*01:06-DQB1*06:38',
         'HLA-DQA1*01:06-DQB1*06:39', 'HLA-DQA1*01:06-DQB1*06:40',
         'HLA-DQA1*01:06-DQB1*06:41', 'HLA-DQA1*01:06-DQB1*06:42', 'HLA-DQA1*01:06-DQB1*06:43', 'HLA-DQA1*01:06-DQB1*06:44',
         'HLA-DQA1*01:07-DQB1*02:01', 'HLA-DQA1*01:07-DQB1*02:02',
         'HLA-DQA1*01:07-DQB1*02:03', 'HLA-DQA1*01:07-DQB1*02:04', 'HLA-DQA1*01:07-DQB1*02:05', 'HLA-DQA1*01:07-DQB1*02:06',
         'HLA-DQA1*01:07-DQB1*03:01', 'HLA-DQA1*01:07-DQB1*03:02',
         'HLA-DQA1*01:07-DQB1*03:03', 'HLA-DQA1*01:07-DQB1*03:04', 'HLA-DQA1*01:07-DQB1*03:05', 'HLA-DQA1*01:07-DQB1*03:06',
         'HLA-DQA1*01:07-DQB1*03:07', 'HLA-DQA1*01:07-DQB1*03:08',
         'HLA-DQA1*01:07-DQB1*03:09', 'HLA-DQA1*01:07-DQB1*03:10', 'HLA-DQA1*01:07-DQB1*03:11', 'HLA-DQA1*01:07-DQB1*03:12',
         'HLA-DQA1*01:07-DQB1*03:13', 'HLA-DQA1*01:07-DQB1*03:14',
         'HLA-DQA1*01:07-DQB1*03:15', 'HLA-DQA1*01:07-DQB1*03:16', 'HLA-DQA1*01:07-DQB1*03:17', 'HLA-DQA1*01:07-DQB1*03:18',
         'HLA-DQA1*01:07-DQB1*03:19', 'HLA-DQA1*01:07-DQB1*03:20',
         'HLA-DQA1*01:07-DQB1*03:21', 'HLA-DQA1*01:07-DQB1*03:22', 'HLA-DQA1*01:07-DQB1*03:23', 'HLA-DQA1*01:07-DQB1*03:24',
         'HLA-DQA1*01:07-DQB1*03:25', 'HLA-DQA1*01:07-DQB1*03:26',
         'HLA-DQA1*01:07-DQB1*03:27', 'HLA-DQA1*01:07-DQB1*03:28', 'HLA-DQA1*01:07-DQB1*03:29', 'HLA-DQA1*01:07-DQB1*03:30',
         'HLA-DQA1*01:07-DQB1*03:31', 'HLA-DQA1*01:07-DQB1*03:32',
         'HLA-DQA1*01:07-DQB1*03:33', 'HLA-DQA1*01:07-DQB1*03:34', 'HLA-DQA1*01:07-DQB1*03:35', 'HLA-DQA1*01:07-DQB1*03:36',
         'HLA-DQA1*01:07-DQB1*03:37', 'HLA-DQA1*01:07-DQB1*03:38',
         'HLA-DQA1*01:07-DQB1*04:01', 'HLA-DQA1*01:07-DQB1*04:02', 'HLA-DQA1*01:07-DQB1*04:03', 'HLA-DQA1*01:07-DQB1*04:04',
         'HLA-DQA1*01:07-DQB1*04:05', 'HLA-DQA1*01:07-DQB1*04:06',
         'HLA-DQA1*01:07-DQB1*04:07', 'HLA-DQA1*01:07-DQB1*04:08', 'HLA-DQA1*01:07-DQB1*05:01', 'HLA-DQA1*01:07-DQB1*05:02',
         'HLA-DQA1*01:07-DQB1*05:03', 'HLA-DQA1*01:07-DQB1*05:05',
         'HLA-DQA1*01:07-DQB1*05:06', 'HLA-DQA1*01:07-DQB1*05:07', 'HLA-DQA1*01:07-DQB1*05:08', 'HLA-DQA1*01:07-DQB1*05:09',
         'HLA-DQA1*01:07-DQB1*05:10', 'HLA-DQA1*01:07-DQB1*05:11',
         'HLA-DQA1*01:07-DQB1*05:12', 'HLA-DQA1*01:07-DQB1*05:13', 'HLA-DQA1*01:07-DQB1*05:14', 'HLA-DQA1*01:07-DQB1*06:01',
         'HLA-DQA1*01:07-DQB1*06:02', 'HLA-DQA1*01:07-DQB1*06:03',
         'HLA-DQA1*01:07-DQB1*06:04', 'HLA-DQA1*01:07-DQB1*06:07', 'HLA-DQA1*01:07-DQB1*06:08', 'HLA-DQA1*01:07-DQB1*06:09',
         'HLA-DQA1*01:07-DQB1*06:10', 'HLA-DQA1*01:07-DQB1*06:11',
         'HLA-DQA1*01:07-DQB1*06:12', 'HLA-DQA1*01:07-DQB1*06:14', 'HLA-DQA1*01:07-DQB1*06:15', 'HLA-DQA1*01:07-DQB1*06:16',
         'HLA-DQA1*01:07-DQB1*06:17', 'HLA-DQA1*01:07-DQB1*06:18',
         'HLA-DQA1*01:07-DQB1*06:19', 'HLA-DQA1*01:07-DQB1*06:21', 'HLA-DQA1*01:07-DQB1*06:22', 'HLA-DQA1*01:07-DQB1*06:23',
         'HLA-DQA1*01:07-DQB1*06:24', 'HLA-DQA1*01:07-DQB1*06:25',
         'HLA-DQA1*01:07-DQB1*06:27', 'HLA-DQA1*01:07-DQB1*06:28', 'HLA-DQA1*01:07-DQB1*06:29', 'HLA-DQA1*01:07-DQB1*06:30',
         'HLA-DQA1*01:07-DQB1*06:31', 'HLA-DQA1*01:07-DQB1*06:32',
         'HLA-DQA1*01:07-DQB1*06:33', 'HLA-DQA1*01:07-DQB1*06:34', 'HLA-DQA1*01:07-DQB1*06:35', 'HLA-DQA1*01:07-DQB1*06:36',
         'HLA-DQA1*01:07-DQB1*06:37', 'HLA-DQA1*01:07-DQB1*06:38',
         'HLA-DQA1*01:07-DQB1*06:39', 'HLA-DQA1*01:07-DQB1*06:40', 'HLA-DQA1*01:07-DQB1*06:41', 'HLA-DQA1*01:07-DQB1*06:42',
         'HLA-DQA1*01:07-DQB1*06:43', 'HLA-DQA1*01:07-DQB1*06:44',
         'HLA-DQA1*01:08-DQB1*02:01', 'HLA-DQA1*01:08-DQB1*02:02', 'HLA-DQA1*01:08-DQB1*02:03', 'HLA-DQA1*01:08-DQB1*02:04',
         'HLA-DQA1*01:08-DQB1*02:05', 'HLA-DQA1*01:08-DQB1*02:06',
         'HLA-DQA1*01:08-DQB1*03:01', 'HLA-DQA1*01:08-DQB1*03:02', 'HLA-DQA1*01:08-DQB1*03:03', 'HLA-DQA1*01:08-DQB1*03:04',
         'HLA-DQA1*01:08-DQB1*03:05', 'HLA-DQA1*01:08-DQB1*03:06',
         'HLA-DQA1*01:08-DQB1*03:07', 'HLA-DQA1*01:08-DQB1*03:08', 'HLA-DQA1*01:08-DQB1*03:09', 'HLA-DQA1*01:08-DQB1*03:10',
         'HLA-DQA1*01:08-DQB1*03:11', 'HLA-DQA1*01:08-DQB1*03:12',
         'HLA-DQA1*01:08-DQB1*03:13', 'HLA-DQA1*01:08-DQB1*03:14', 'HLA-DQA1*01:08-DQB1*03:15', 'HLA-DQA1*01:08-DQB1*03:16',
         'HLA-DQA1*01:08-DQB1*03:17', 'HLA-DQA1*01:08-DQB1*03:18',
         'HLA-DQA1*01:08-DQB1*03:19', 'HLA-DQA1*01:08-DQB1*03:20', 'HLA-DQA1*01:08-DQB1*03:21', 'HLA-DQA1*01:08-DQB1*03:22',
         'HLA-DQA1*01:08-DQB1*03:23', 'HLA-DQA1*01:08-DQB1*03:24',
         'HLA-DQA1*01:08-DQB1*03:25', 'HLA-DQA1*01:08-DQB1*03:26', 'HLA-DQA1*01:08-DQB1*03:27', 'HLA-DQA1*01:08-DQB1*03:28',
         'HLA-DQA1*01:08-DQB1*03:29', 'HLA-DQA1*01:08-DQB1*03:30',
         'HLA-DQA1*01:08-DQB1*03:31', 'HLA-DQA1*01:08-DQB1*03:32', 'HLA-DQA1*01:08-DQB1*03:33', 'HLA-DQA1*01:08-DQB1*03:34',
         'HLA-DQA1*01:08-DQB1*03:35', 'HLA-DQA1*01:08-DQB1*03:36',
         'HLA-DQA1*01:08-DQB1*03:37', 'HLA-DQA1*01:08-DQB1*03:38', 'HLA-DQA1*01:08-DQB1*04:01', 'HLA-DQA1*01:08-DQB1*04:02',
         'HLA-DQA1*01:08-DQB1*04:03', 'HLA-DQA1*01:08-DQB1*04:04',
         'HLA-DQA1*01:08-DQB1*04:05', 'HLA-DQA1*01:08-DQB1*04:06', 'HLA-DQA1*01:08-DQB1*04:07', 'HLA-DQA1*01:08-DQB1*04:08',
         'HLA-DQA1*01:08-DQB1*05:01', 'HLA-DQA1*01:08-DQB1*05:02',
         'HLA-DQA1*01:08-DQB1*05:03', 'HLA-DQA1*01:08-DQB1*05:05', 'HLA-DQA1*01:08-DQB1*05:06', 'HLA-DQA1*01:08-DQB1*05:07',
         'HLA-DQA1*01:08-DQB1*05:08', 'HLA-DQA1*01:08-DQB1*05:09',
         'HLA-DQA1*01:08-DQB1*05:10', 'HLA-DQA1*01:08-DQB1*05:11', 'HLA-DQA1*01:08-DQB1*05:12', 'HLA-DQA1*01:08-DQB1*05:13',
         'HLA-DQA1*01:08-DQB1*05:14', 'HLA-DQA1*01:08-DQB1*06:01',
         'HLA-DQA1*01:08-DQB1*06:02', 'HLA-DQA1*01:08-DQB1*06:03', 'HLA-DQA1*01:08-DQB1*06:04', 'HLA-DQA1*01:08-DQB1*06:07',
         'HLA-DQA1*01:08-DQB1*06:08', 'HLA-DQA1*01:08-DQB1*06:09',
         'HLA-DQA1*01:08-DQB1*06:10', 'HLA-DQA1*01:08-DQB1*06:11', 'HLA-DQA1*01:08-DQB1*06:12', 'HLA-DQA1*01:08-DQB1*06:14',
         'HLA-DQA1*01:08-DQB1*06:15', 'HLA-DQA1*01:08-DQB1*06:16',
         'HLA-DQA1*01:08-DQB1*06:17', 'HLA-DQA1*01:08-DQB1*06:18', 'HLA-DQA1*01:08-DQB1*06:19', 'HLA-DQA1*01:08-DQB1*06:21',
         'HLA-DQA1*01:08-DQB1*06:22', 'HLA-DQA1*01:08-DQB1*06:23',
         'HLA-DQA1*01:08-DQB1*06:24', 'HLA-DQA1*01:08-DQB1*06:25', 'HLA-DQA1*01:08-DQB1*06:27', 'HLA-DQA1*01:08-DQB1*06:28',
         'HLA-DQA1*01:08-DQB1*06:29', 'HLA-DQA1*01:08-DQB1*06:30',
         'HLA-DQA1*01:08-DQB1*06:31', 'HLA-DQA1*01:08-DQB1*06:32', 'HLA-DQA1*01:08-DQB1*06:33', 'HLA-DQA1*01:08-DQB1*06:34',
         'HLA-DQA1*01:08-DQB1*06:35', 'HLA-DQA1*01:08-DQB1*06:36',
         'HLA-DQA1*01:08-DQB1*06:37', 'HLA-DQA1*01:08-DQB1*06:38', 'HLA-DQA1*01:08-DQB1*06:39', 'HLA-DQA1*01:08-DQB1*06:40',
         'HLA-DQA1*01:08-DQB1*06:41', 'HLA-DQA1*01:08-DQB1*06:42',
         'HLA-DQA1*01:08-DQB1*06:43', 'HLA-DQA1*01:08-DQB1*06:44', 'HLA-DQA1*01:09-DQB1*02:01', 'HLA-DQA1*01:09-DQB1*02:02',
         'HLA-DQA1*01:09-DQB1*02:03', 'HLA-DQA1*01:09-DQB1*02:04',
         'HLA-DQA1*01:09-DQB1*02:05', 'HLA-DQA1*01:09-DQB1*02:06', 'HLA-DQA1*01:09-DQB1*03:01', 'HLA-DQA1*01:09-DQB1*03:02',
         'HLA-DQA1*01:09-DQB1*03:03', 'HLA-DQA1*01:09-DQB1*03:04',
         'HLA-DQA1*01:09-DQB1*03:05', 'HLA-DQA1*01:09-DQB1*03:06', 'HLA-DQA1*01:09-DQB1*03:07', 'HLA-DQA1*01:09-DQB1*03:08',
         'HLA-DQA1*01:09-DQB1*03:09', 'HLA-DQA1*01:09-DQB1*03:10',
         'HLA-DQA1*01:09-DQB1*03:11', 'HLA-DQA1*01:09-DQB1*03:12', 'HLA-DQA1*01:09-DQB1*03:13', 'HLA-DQA1*01:09-DQB1*03:14',
         'HLA-DQA1*01:09-DQB1*03:15', 'HLA-DQA1*01:09-DQB1*03:16',
         'HLA-DQA1*01:09-DQB1*03:17', 'HLA-DQA1*01:09-DQB1*03:18', 'HLA-DQA1*01:09-DQB1*03:19', 'HLA-DQA1*01:09-DQB1*03:20',
         'HLA-DQA1*01:09-DQB1*03:21', 'HLA-DQA1*01:09-DQB1*03:22',
         'HLA-DQA1*01:09-DQB1*03:23', 'HLA-DQA1*01:09-DQB1*03:24', 'HLA-DQA1*01:09-DQB1*03:25', 'HLA-DQA1*01:09-DQB1*03:26',
         'HLA-DQA1*01:09-DQB1*03:27', 'HLA-DQA1*01:09-DQB1*03:28',
         'HLA-DQA1*01:09-DQB1*03:29', 'HLA-DQA1*01:09-DQB1*03:30', 'HLA-DQA1*01:09-DQB1*03:31', 'HLA-DQA1*01:09-DQB1*03:32',
         'HLA-DQA1*01:09-DQB1*03:33', 'HLA-DQA1*01:09-DQB1*03:34',
         'HLA-DQA1*01:09-DQB1*03:35', 'HLA-DQA1*01:09-DQB1*03:36', 'HLA-DQA1*01:09-DQB1*03:37', 'HLA-DQA1*01:09-DQB1*03:38',
         'HLA-DQA1*01:09-DQB1*04:01', 'HLA-DQA1*01:09-DQB1*04:02',
         'HLA-DQA1*01:09-DQB1*04:03', 'HLA-DQA1*01:09-DQB1*04:04', 'HLA-DQA1*01:09-DQB1*04:05', 'HLA-DQA1*01:09-DQB1*04:06',
         'HLA-DQA1*01:09-DQB1*04:07', 'HLA-DQA1*01:09-DQB1*04:08',
         'HLA-DQA1*01:09-DQB1*05:01', 'HLA-DQA1*01:09-DQB1*05:02', 'HLA-DQA1*01:09-DQB1*05:03', 'HLA-DQA1*01:09-DQB1*05:05',
         'HLA-DQA1*01:09-DQB1*05:06', 'HLA-DQA1*01:09-DQB1*05:07',
         'HLA-DQA1*01:09-DQB1*05:08', 'HLA-DQA1*01:09-DQB1*05:09', 'HLA-DQA1*01:09-DQB1*05:10', 'HLA-DQA1*01:09-DQB1*05:11',
         'HLA-DQA1*01:09-DQB1*05:12', 'HLA-DQA1*01:09-DQB1*05:13',
         'HLA-DQA1*01:09-DQB1*05:14', 'HLA-DQA1*01:09-DQB1*06:01', 'HLA-DQA1*01:09-DQB1*06:02', 'HLA-DQA1*01:09-DQB1*06:03',
         'HLA-DQA1*01:09-DQB1*06:04', 'HLA-DQA1*01:09-DQB1*06:07',
         'HLA-DQA1*01:09-DQB1*06:08', 'HLA-DQA1*01:09-DQB1*06:09', 'HLA-DQA1*01:09-DQB1*06:10', 'HLA-DQA1*01:09-DQB1*06:11',
         'HLA-DQA1*01:09-DQB1*06:12', 'HLA-DQA1*01:09-DQB1*06:14',
         'HLA-DQA1*01:09-DQB1*06:15', 'HLA-DQA1*01:09-DQB1*06:16', 'HLA-DQA1*01:09-DQB1*06:17', 'HLA-DQA1*01:09-DQB1*06:18',
         'HLA-DQA1*01:09-DQB1*06:19', 'HLA-DQA1*01:09-DQB1*06:21',
         'HLA-DQA1*01:09-DQB1*06:22', 'HLA-DQA1*01:09-DQB1*06:23', 'HLA-DQA1*01:09-DQB1*06:24', 'HLA-DQA1*01:09-DQB1*06:25',
         'HLA-DQA1*01:09-DQB1*06:27', 'HLA-DQA1*01:09-DQB1*06:28',
         'HLA-DQA1*01:09-DQB1*06:29', 'HLA-DQA1*01:09-DQB1*06:30', 'HLA-DQA1*01:09-DQB1*06:31', 'HLA-DQA1*01:09-DQB1*06:32',
         'HLA-DQA1*01:09-DQB1*06:33', 'HLA-DQA1*01:09-DQB1*06:34',
         'HLA-DQA1*01:09-DQB1*06:35', 'HLA-DQA1*01:09-DQB1*06:36', 'HLA-DQA1*01:09-DQB1*06:37', 'HLA-DQA1*01:09-DQB1*06:38',
         'HLA-DQA1*01:09-DQB1*06:39', 'HLA-DQA1*01:09-DQB1*06:40',
         'HLA-DQA1*01:09-DQB1*06:41', 'HLA-DQA1*01:09-DQB1*06:42', 'HLA-DQA1*01:09-DQB1*06:43', 'HLA-DQA1*01:09-DQB1*06:44',
         'HLA-DQA1*02:01-DQB1*02:01', 'HLA-DQA1*02:01-DQB1*02:02',
         'HLA-DQA1*02:01-DQB1*02:03', 'HLA-DQA1*02:01-DQB1*02:04', 'HLA-DQA1*02:01-DQB1*02:05', 'HLA-DQA1*02:01-DQB1*02:06',
         'HLA-DQA1*02:01-DQB1*03:01', 'HLA-DQA1*02:01-DQB1*03:02',
         'HLA-DQA1*02:01-DQB1*03:03', 'HLA-DQA1*02:01-DQB1*03:04', 'HLA-DQA1*02:01-DQB1*03:05', 'HLA-DQA1*02:01-DQB1*03:06',
         'HLA-DQA1*02:01-DQB1*03:07', 'HLA-DQA1*02:01-DQB1*03:08',
         'HLA-DQA1*02:01-DQB1*03:09', 'HLA-DQA1*02:01-DQB1*03:10', 'HLA-DQA1*02:01-DQB1*03:11', 'HLA-DQA1*02:01-DQB1*03:12',
         'HLA-DQA1*02:01-DQB1*03:13', 'HLA-DQA1*02:01-DQB1*03:14',
         'HLA-DQA1*02:01-DQB1*03:15', 'HLA-DQA1*02:01-DQB1*03:16', 'HLA-DQA1*02:01-DQB1*03:17', 'HLA-DQA1*02:01-DQB1*03:18',
         'HLA-DQA1*02:01-DQB1*03:19', 'HLA-DQA1*02:01-DQB1*03:20',
         'HLA-DQA1*02:01-DQB1*03:21', 'HLA-DQA1*02:01-DQB1*03:22', 'HLA-DQA1*02:01-DQB1*03:23', 'HLA-DQA1*02:01-DQB1*03:24',
         'HLA-DQA1*02:01-DQB1*03:25', 'HLA-DQA1*02:01-DQB1*03:26',
         'HLA-DQA1*02:01-DQB1*03:27', 'HLA-DQA1*02:01-DQB1*03:28', 'HLA-DQA1*02:01-DQB1*03:29', 'HLA-DQA1*02:01-DQB1*03:30',
         'HLA-DQA1*02:01-DQB1*03:31', 'HLA-DQA1*02:01-DQB1*03:32',
         'HLA-DQA1*02:01-DQB1*03:33', 'HLA-DQA1*02:01-DQB1*03:34', 'HLA-DQA1*02:01-DQB1*03:35', 'HLA-DQA1*02:01-DQB1*03:36',
         'HLA-DQA1*02:01-DQB1*03:37', 'HLA-DQA1*02:01-DQB1*03:38',
         'HLA-DQA1*02:01-DQB1*04:01', 'HLA-DQA1*02:01-DQB1*04:02', 'HLA-DQA1*02:01-DQB1*04:03', 'HLA-DQA1*02:01-DQB1*04:04',
         'HLA-DQA1*02:01-DQB1*04:05', 'HLA-DQA1*02:01-DQB1*04:06',
         'HLA-DQA1*02:01-DQB1*04:07', 'HLA-DQA1*02:01-DQB1*04:08', 'HLA-DQA1*02:01-DQB1*05:01', 'HLA-DQA1*02:01-DQB1*05:02',
         'HLA-DQA1*02:01-DQB1*05:03', 'HLA-DQA1*02:01-DQB1*05:05',
         'HLA-DQA1*02:01-DQB1*05:06', 'HLA-DQA1*02:01-DQB1*05:07', 'HLA-DQA1*02:01-DQB1*05:08', 'HLA-DQA1*02:01-DQB1*05:09',
         'HLA-DQA1*02:01-DQB1*05:10', 'HLA-DQA1*02:01-DQB1*05:11',
         'HLA-DQA1*02:01-DQB1*05:12', 'HLA-DQA1*02:01-DQB1*05:13', 'HLA-DQA1*02:01-DQB1*05:14', 'HLA-DQA1*02:01-DQB1*06:01',
         'HLA-DQA1*02:01-DQB1*06:02', 'HLA-DQA1*02:01-DQB1*06:03',
         'HLA-DQA1*02:01-DQB1*06:04', 'HLA-DQA1*02:01-DQB1*06:07', 'HLA-DQA1*02:01-DQB1*06:08', 'HLA-DQA1*02:01-DQB1*06:09',
         'HLA-DQA1*02:01-DQB1*06:10', 'HLA-DQA1*02:01-DQB1*06:11',
         'HLA-DQA1*02:01-DQB1*06:12', 'HLA-DQA1*02:01-DQB1*06:14', 'HLA-DQA1*02:01-DQB1*06:15', 'HLA-DQA1*02:01-DQB1*06:16',
         'HLA-DQA1*02:01-DQB1*06:17', 'HLA-DQA1*02:01-DQB1*06:18',
         'HLA-DQA1*02:01-DQB1*06:19', 'HLA-DQA1*02:01-DQB1*06:21', 'HLA-DQA1*02:01-DQB1*06:22', 'HLA-DQA1*02:01-DQB1*06:23',
         'HLA-DQA1*02:01-DQB1*06:24', 'HLA-DQA1*02:01-DQB1*06:25',
         'HLA-DQA1*02:01-DQB1*06:27', 'HLA-DQA1*02:01-DQB1*06:28', 'HLA-DQA1*02:01-DQB1*06:29', 'HLA-DQA1*02:01-DQB1*06:30',
         'HLA-DQA1*02:01-DQB1*06:31', 'HLA-DQA1*02:01-DQB1*06:32',
         'HLA-DQA1*02:01-DQB1*06:33', 'HLA-DQA1*02:01-DQB1*06:34', 'HLA-DQA1*02:01-DQB1*06:35', 'HLA-DQA1*02:01-DQB1*06:36',
         'HLA-DQA1*02:01-DQB1*06:37', 'HLA-DQA1*02:01-DQB1*06:38',
         'HLA-DQA1*02:01-DQB1*06:39', 'HLA-DQA1*02:01-DQB1*06:40', 'HLA-DQA1*02:01-DQB1*06:41', 'HLA-DQA1*02:01-DQB1*06:42',
         'HLA-DQA1*02:01-DQB1*06:43', 'HLA-DQA1*02:01-DQB1*06:44',
         'HLA-DQA1*03:01-DQB1*02:01', 'HLA-DQA1*03:01-DQB1*02:02', 'HLA-DQA1*03:01-DQB1*02:03', 'HLA-DQA1*03:01-DQB1*02:04',
         'HLA-DQA1*03:01-DQB1*02:05', 'HLA-DQA1*03:01-DQB1*02:06',
         'HLA-DQA1*03:01-DQB1*03:01', 'HLA-DQA1*03:01-DQB1*03:02', 'HLA-DQA1*03:01-DQB1*03:03', 'HLA-DQA1*03:01-DQB1*03:04',
         'HLA-DQA1*03:01-DQB1*03:05', 'HLA-DQA1*03:01-DQB1*03:06',
         'HLA-DQA1*03:01-DQB1*03:07', 'HLA-DQA1*03:01-DQB1*03:08', 'HLA-DQA1*03:01-DQB1*03:09', 'HLA-DQA1*03:01-DQB1*03:10',
         'HLA-DQA1*03:01-DQB1*03:11', 'HLA-DQA1*03:01-DQB1*03:12',
         'HLA-DQA1*03:01-DQB1*03:13', 'HLA-DQA1*03:01-DQB1*03:14', 'HLA-DQA1*03:01-DQB1*03:15', 'HLA-DQA1*03:01-DQB1*03:16',
         'HLA-DQA1*03:01-DQB1*03:17', 'HLA-DQA1*03:01-DQB1*03:18',
         'HLA-DQA1*03:01-DQB1*03:19', 'HLA-DQA1*03:01-DQB1*03:20', 'HLA-DQA1*03:01-DQB1*03:21', 'HLA-DQA1*03:01-DQB1*03:22',
         'HLA-DQA1*03:01-DQB1*03:23', 'HLA-DQA1*03:01-DQB1*03:24',
         'HLA-DQA1*03:01-DQB1*03:25', 'HLA-DQA1*03:01-DQB1*03:26', 'HLA-DQA1*03:01-DQB1*03:27', 'HLA-DQA1*03:01-DQB1*03:28',
         'HLA-DQA1*03:01-DQB1*03:29', 'HLA-DQA1*03:01-DQB1*03:30',
         'HLA-DQA1*03:01-DQB1*03:31', 'HLA-DQA1*03:01-DQB1*03:32', 'HLA-DQA1*03:01-DQB1*03:33', 'HLA-DQA1*03:01-DQB1*03:34',
         'HLA-DQA1*03:01-DQB1*03:35', 'HLA-DQA1*03:01-DQB1*03:36',
         'HLA-DQA1*03:01-DQB1*03:37', 'HLA-DQA1*03:01-DQB1*03:38', 'HLA-DQA1*03:01-DQB1*04:01', 'HLA-DQA1*03:01-DQB1*04:02',
         'HLA-DQA1*03:01-DQB1*04:03', 'HLA-DQA1*03:01-DQB1*04:04',
         'HLA-DQA1*03:01-DQB1*04:05', 'HLA-DQA1*03:01-DQB1*04:06', 'HLA-DQA1*03:01-DQB1*04:07', 'HLA-DQA1*03:01-DQB1*04:08',
         'HLA-DQA1*03:01-DQB1*05:01', 'HLA-DQA1*03:01-DQB1*05:02',
         'HLA-DQA1*03:01-DQB1*05:03', 'HLA-DQA1*03:01-DQB1*05:05', 'HLA-DQA1*03:01-DQB1*05:06', 'HLA-DQA1*03:01-DQB1*05:07',
         'HLA-DQA1*03:01-DQB1*05:08', 'HLA-DQA1*03:01-DQB1*05:09',
         'HLA-DQA1*03:01-DQB1*05:10', 'HLA-DQA1*03:01-DQB1*05:11', 'HLA-DQA1*03:01-DQB1*05:12', 'HLA-DQA1*03:01-DQB1*05:13',
         'HLA-DQA1*03:01-DQB1*05:14', 'HLA-DQA1*03:01-DQB1*06:01',
         'HLA-DQA1*03:01-DQB1*06:02', 'HLA-DQA1*03:01-DQB1*06:03', 'HLA-DQA1*03:01-DQB1*06:04', 'HLA-DQA1*03:01-DQB1*06:07',
         'HLA-DQA1*03:01-DQB1*06:08', 'HLA-DQA1*03:01-DQB1*06:09',
         'HLA-DQA1*03:01-DQB1*06:10', 'HLA-DQA1*03:01-DQB1*06:11', 'HLA-DQA1*03:01-DQB1*06:12', 'HLA-DQA1*03:01-DQB1*06:14',
         'HLA-DQA1*03:01-DQB1*06:15', 'HLA-DQA1*03:01-DQB1*06:16',
         'HLA-DQA1*03:01-DQB1*06:17', 'HLA-DQA1*03:01-DQB1*06:18', 'HLA-DQA1*03:01-DQB1*06:19', 'HLA-DQA1*03:01-DQB1*06:21',
         'HLA-DQA1*03:01-DQB1*06:22', 'HLA-DQA1*03:01-DQB1*06:23',
         'HLA-DQA1*03:01-DQB1*06:24', 'HLA-DQA1*03:01-DQB1*06:25', 'HLA-DQA1*03:01-DQB1*06:27', 'HLA-DQA1*03:01-DQB1*06:28',
         'HLA-DQA1*03:01-DQB1*06:29', 'HLA-DQA1*03:01-DQB1*06:30',
         'HLA-DQA1*03:01-DQB1*06:31', 'HLA-DQA1*03:01-DQB1*06:32', 'HLA-DQA1*03:01-DQB1*06:33', 'HLA-DQA1*03:01-DQB1*06:34',
         'HLA-DQA1*03:01-DQB1*06:35', 'HLA-DQA1*03:01-DQB1*06:36',
         'HLA-DQA1*03:01-DQB1*06:37', 'HLA-DQA1*03:01-DQB1*06:38', 'HLA-DQA1*03:01-DQB1*06:39', 'HLA-DQA1*03:01-DQB1*06:40',
         'HLA-DQA1*03:01-DQB1*06:41', 'HLA-DQA1*03:01-DQB1*06:42',
         'HLA-DQA1*03:01-DQB1*06:43', 'HLA-DQA1*03:01-DQB1*06:44', 'HLA-DQA1*03:02-DQB1*02:01', 'HLA-DQA1*03:02-DQB1*02:02',
         'HLA-DQA1*03:02-DQB1*02:03', 'HLA-DQA1*03:02-DQB1*02:04',
         'HLA-DQA1*03:02-DQB1*02:05', 'HLA-DQA1*03:02-DQB1*02:06', 'HLA-DQA1*03:02-DQB1*03:01', 'HLA-DQA1*03:02-DQB1*03:02',
         'HLA-DQA1*03:02-DQB1*03:03', 'HLA-DQA1*03:02-DQB1*03:04',
         'HLA-DQA1*03:02-DQB1*03:05', 'HLA-DQA1*03:02-DQB1*03:06', 'HLA-DQA1*03:02-DQB1*03:07', 'HLA-DQA1*03:02-DQB1*03:08',
         'HLA-DQA1*03:02-DQB1*03:09', 'HLA-DQA1*03:02-DQB1*03:10',
         'HLA-DQA1*03:02-DQB1*03:11', 'HLA-DQA1*03:02-DQB1*03:12', 'HLA-DQA1*03:02-DQB1*03:13', 'HLA-DQA1*03:02-DQB1*03:14',
         'HLA-DQA1*03:02-DQB1*03:15', 'HLA-DQA1*03:02-DQB1*03:16',
         'HLA-DQA1*03:02-DQB1*03:17', 'HLA-DQA1*03:02-DQB1*03:18', 'HLA-DQA1*03:02-DQB1*03:19', 'HLA-DQA1*03:02-DQB1*03:20',
         'HLA-DQA1*03:02-DQB1*03:21', 'HLA-DQA1*03:02-DQB1*03:22',
         'HLA-DQA1*03:02-DQB1*03:23', 'HLA-DQA1*03:02-DQB1*03:24', 'HLA-DQA1*03:02-DQB1*03:25', 'HLA-DQA1*03:02-DQB1*03:26',
         'HLA-DQA1*03:02-DQB1*03:27', 'HLA-DQA1*03:02-DQB1*03:28',
         'HLA-DQA1*03:02-DQB1*03:29', 'HLA-DQA1*03:02-DQB1*03:30', 'HLA-DQA1*03:02-DQB1*03:31', 'HLA-DQA1*03:02-DQB1*03:32',
         'HLA-DQA1*03:02-DQB1*03:33', 'HLA-DQA1*03:02-DQB1*03:34',
         'HLA-DQA1*03:02-DQB1*03:35', 'HLA-DQA1*03:02-DQB1*03:36', 'HLA-DQA1*03:02-DQB1*03:37', 'HLA-DQA1*03:02-DQB1*03:38',
         'HLA-DQA1*03:02-DQB1*04:01', 'HLA-DQA1*03:02-DQB1*04:02',
         'HLA-DQA1*03:02-DQB1*04:03', 'HLA-DQA1*03:02-DQB1*04:04', 'HLA-DQA1*03:02-DQB1*04:05', 'HLA-DQA1*03:02-DQB1*04:06',
         'HLA-DQA1*03:02-DQB1*04:07', 'HLA-DQA1*03:02-DQB1*04:08',
         'HLA-DQA1*03:02-DQB1*05:01', 'HLA-DQA1*03:02-DQB1*05:02', 'HLA-DQA1*03:02-DQB1*05:03', 'HLA-DQA1*03:02-DQB1*05:05',
         'HLA-DQA1*03:02-DQB1*05:06', 'HLA-DQA1*03:02-DQB1*05:07',
         'HLA-DQA1*03:02-DQB1*05:08', 'HLA-DQA1*03:02-DQB1*05:09', 'HLA-DQA1*03:02-DQB1*05:10', 'HLA-DQA1*03:02-DQB1*05:11',
         'HLA-DQA1*03:02-DQB1*05:12', 'HLA-DQA1*03:02-DQB1*05:13',
         'HLA-DQA1*03:02-DQB1*05:14', 'HLA-DQA1*03:02-DQB1*06:01', 'HLA-DQA1*03:02-DQB1*06:02', 'HLA-DQA1*03:02-DQB1*06:03',
         'HLA-DQA1*03:02-DQB1*06:04', 'HLA-DQA1*03:02-DQB1*06:07',
         'HLA-DQA1*03:02-DQB1*06:08', 'HLA-DQA1*03:02-DQB1*06:09', 'HLA-DQA1*03:02-DQB1*06:10', 'HLA-DQA1*03:02-DQB1*06:11',
         'HLA-DQA1*03:02-DQB1*06:12', 'HLA-DQA1*03:02-DQB1*06:14',
         'HLA-DQA1*03:02-DQB1*06:15', 'HLA-DQA1*03:02-DQB1*06:16', 'HLA-DQA1*03:02-DQB1*06:17', 'HLA-DQA1*03:02-DQB1*06:18',
         'HLA-DQA1*03:02-DQB1*06:19', 'HLA-DQA1*03:02-DQB1*06:21',
         'HLA-DQA1*03:02-DQB1*06:22', 'HLA-DQA1*03:02-DQB1*06:23', 'HLA-DQA1*03:02-DQB1*06:24', 'HLA-DQA1*03:02-DQB1*06:25',
         'HLA-DQA1*03:02-DQB1*06:27', 'HLA-DQA1*03:02-DQB1*06:28',
         'HLA-DQA1*03:02-DQB1*06:29', 'HLA-DQA1*03:02-DQB1*06:30', 'HLA-DQA1*03:02-DQB1*06:31', 'HLA-DQA1*03:02-DQB1*06:32',
         'HLA-DQA1*03:02-DQB1*06:33', 'HLA-DQA1*03:02-DQB1*06:34',
         'HLA-DQA1*03:02-DQB1*06:35', 'HLA-DQA1*03:02-DQB1*06:36', 'HLA-DQA1*03:02-DQB1*06:37', 'HLA-DQA1*03:02-DQB1*06:38',
         'HLA-DQA1*03:02-DQB1*06:39', 'HLA-DQA1*03:02-DQB1*06:40',
         'HLA-DQA1*03:02-DQB1*06:41', 'HLA-DQA1*03:02-DQB1*06:42', 'HLA-DQA1*03:02-DQB1*06:43', 'HLA-DQA1*03:02-DQB1*06:44',
         'HLA-DQA1*03:03-DQB1*02:01', 'HLA-DQA1*03:03-DQB1*02:02',
         'HLA-DQA1*03:03-DQB1*02:03', 'HLA-DQA1*03:03-DQB1*02:04', 'HLA-DQA1*03:03-DQB1*02:05', 'HLA-DQA1*03:03-DQB1*02:06',
         'HLA-DQA1*03:03-DQB1*03:01', 'HLA-DQA1*03:03-DQB1*03:02',
         'HLA-DQA1*03:03-DQB1*03:03', 'HLA-DQA1*03:03-DQB1*03:04', 'HLA-DQA1*03:03-DQB1*03:05', 'HLA-DQA1*03:03-DQB1*03:06',
         'HLA-DQA1*03:03-DQB1*03:07', 'HLA-DQA1*03:03-DQB1*03:08',
         'HLA-DQA1*03:03-DQB1*03:09', 'HLA-DQA1*03:03-DQB1*03:10', 'HLA-DQA1*03:03-DQB1*03:11', 'HLA-DQA1*03:03-DQB1*03:12',
         'HLA-DQA1*03:03-DQB1*03:13', 'HLA-DQA1*03:03-DQB1*03:14',
         'HLA-DQA1*03:03-DQB1*03:15', 'HLA-DQA1*03:03-DQB1*03:16', 'HLA-DQA1*03:03-DQB1*03:17', 'HLA-DQA1*03:03-DQB1*03:18',
         'HLA-DQA1*03:03-DQB1*03:19', 'HLA-DQA1*03:03-DQB1*03:20',
         'HLA-DQA1*03:03-DQB1*03:21', 'HLA-DQA1*03:03-DQB1*03:22', 'HLA-DQA1*03:03-DQB1*03:23', 'HLA-DQA1*03:03-DQB1*03:24',
         'HLA-DQA1*03:03-DQB1*03:25', 'HLA-DQA1*03:03-DQB1*03:26',
         'HLA-DQA1*03:03-DQB1*03:27', 'HLA-DQA1*03:03-DQB1*03:28', 'HLA-DQA1*03:03-DQB1*03:29', 'HLA-DQA1*03:03-DQB1*03:30',
         'HLA-DQA1*03:03-DQB1*03:31', 'HLA-DQA1*03:03-DQB1*03:32',
         'HLA-DQA1*03:03-DQB1*03:33', 'HLA-DQA1*03:03-DQB1*03:34', 'HLA-DQA1*03:03-DQB1*03:35', 'HLA-DQA1*03:03-DQB1*03:36',
         'HLA-DQA1*03:03-DQB1*03:37', 'HLA-DQA1*03:03-DQB1*03:38',
         'HLA-DQA1*03:03-DQB1*04:01', 'HLA-DQA1*03:03-DQB1*04:02', 'HLA-DQA1*03:03-DQB1*04:03', 'HLA-DQA1*03:03-DQB1*04:04',
         'HLA-DQA1*03:03-DQB1*04:05', 'HLA-DQA1*03:03-DQB1*04:06',
         'HLA-DQA1*03:03-DQB1*04:07', 'HLA-DQA1*03:03-DQB1*04:08', 'HLA-DQA1*03:03-DQB1*05:01', 'HLA-DQA1*03:03-DQB1*05:02',
         'HLA-DQA1*03:03-DQB1*05:03', 'HLA-DQA1*03:03-DQB1*05:05',
         'HLA-DQA1*03:03-DQB1*05:06', 'HLA-DQA1*03:03-DQB1*05:07', 'HLA-DQA1*03:03-DQB1*05:08', 'HLA-DQA1*03:03-DQB1*05:09',
         'HLA-DQA1*03:03-DQB1*05:10', 'HLA-DQA1*03:03-DQB1*05:11',
         'HLA-DQA1*03:03-DQB1*05:12', 'HLA-DQA1*03:03-DQB1*05:13', 'HLA-DQA1*03:03-DQB1*05:14', 'HLA-DQA1*03:03-DQB1*06:01',
         'HLA-DQA1*03:03-DQB1*06:02', 'HLA-DQA1*03:03-DQB1*06:03',
         'HLA-DQA1*03:03-DQB1*06:04', 'HLA-DQA1*03:03-DQB1*06:07', 'HLA-DQA1*03:03-DQB1*06:08', 'HLA-DQA1*03:03-DQB1*06:09',
         'HLA-DQA1*03:03-DQB1*06:10', 'HLA-DQA1*03:03-DQB1*06:11',
         'HLA-DQA1*03:03-DQB1*06:12', 'HLA-DQA1*03:03-DQB1*06:14', 'HLA-DQA1*03:03-DQB1*06:15', 'HLA-DQA1*03:03-DQB1*06:16',
         'HLA-DQA1*03:03-DQB1*06:17', 'HLA-DQA1*03:03-DQB1*06:18',
         'HLA-DQA1*03:03-DQB1*06:19', 'HLA-DQA1*03:03-DQB1*06:21', 'HLA-DQA1*03:03-DQB1*06:22', 'HLA-DQA1*03:03-DQB1*06:23',
         'HLA-DQA1*03:03-DQB1*06:24', 'HLA-DQA1*03:03-DQB1*06:25',
         'HLA-DQA1*03:03-DQB1*06:27', 'HLA-DQA1*03:03-DQB1*06:28', 'HLA-DQA1*03:03-DQB1*06:29', 'HLA-DQA1*03:03-DQB1*06:30',
         'HLA-DQA1*03:03-DQB1*06:31', 'HLA-DQA1*03:03-DQB1*06:32',
         'HLA-DQA1*03:03-DQB1*06:33', 'HLA-DQA1*03:03-DQB1*06:34', 'HLA-DQA1*03:03-DQB1*06:35', 'HLA-DQA1*03:03-DQB1*06:36',
         'HLA-DQA1*03:03-DQB1*06:37', 'HLA-DQA1*03:03-DQB1*06:38',
         'HLA-DQA1*03:03-DQB1*06:39', 'HLA-DQA1*03:03-DQB1*06:40', 'HLA-DQA1*03:03-DQB1*06:41', 'HLA-DQA1*03:03-DQB1*06:42',
         'HLA-DQA1*03:03-DQB1*06:43', 'HLA-DQA1*03:03-DQB1*06:44',
         'HLA-DQA1*04:01-DQB1*02:01', 'HLA-DQA1*04:01-DQB1*02:02', 'HLA-DQA1*04:01-DQB1*02:03', 'HLA-DQA1*04:01-DQB1*02:04',
         'HLA-DQA1*04:01-DQB1*02:05', 'HLA-DQA1*04:01-DQB1*02:06',
         'HLA-DQA1*04:01-DQB1*03:01', 'HLA-DQA1*04:01-DQB1*03:02', 'HLA-DQA1*04:01-DQB1*03:03', 'HLA-DQA1*04:01-DQB1*03:04',
         'HLA-DQA1*04:01-DQB1*03:05', 'HLA-DQA1*04:01-DQB1*03:06',
         'HLA-DQA1*04:01-DQB1*03:07', 'HLA-DQA1*04:01-DQB1*03:08', 'HLA-DQA1*04:01-DQB1*03:09', 'HLA-DQA1*04:01-DQB1*03:10',
         'HLA-DQA1*04:01-DQB1*03:11', 'HLA-DQA1*04:01-DQB1*03:12',
         'HLA-DQA1*04:01-DQB1*03:13', 'HLA-DQA1*04:01-DQB1*03:14', 'HLA-DQA1*04:01-DQB1*03:15', 'HLA-DQA1*04:01-DQB1*03:16',
         'HLA-DQA1*04:01-DQB1*03:17', 'HLA-DQA1*04:01-DQB1*03:18',
         'HLA-DQA1*04:01-DQB1*03:19', 'HLA-DQA1*04:01-DQB1*03:20', 'HLA-DQA1*04:01-DQB1*03:21', 'HLA-DQA1*04:01-DQB1*03:22',
         'HLA-DQA1*04:01-DQB1*03:23', 'HLA-DQA1*04:01-DQB1*03:24',
         'HLA-DQA1*04:01-DQB1*03:25', 'HLA-DQA1*04:01-DQB1*03:26', 'HLA-DQA1*04:01-DQB1*03:27', 'HLA-DQA1*04:01-DQB1*03:28',
         'HLA-DQA1*04:01-DQB1*03:29', 'HLA-DQA1*04:01-DQB1*03:30',
         'HLA-DQA1*04:01-DQB1*03:31', 'HLA-DQA1*04:01-DQB1*03:32', 'HLA-DQA1*04:01-DQB1*03:33', 'HLA-DQA1*04:01-DQB1*03:34',
         'HLA-DQA1*04:01-DQB1*03:35', 'HLA-DQA1*04:01-DQB1*03:36',
         'HLA-DQA1*04:01-DQB1*03:37', 'HLA-DQA1*04:01-DQB1*03:38', 'HLA-DQA1*04:01-DQB1*04:01', 'HLA-DQA1*04:01-DQB1*04:02',
         'HLA-DQA1*04:01-DQB1*04:03', 'HLA-DQA1*04:01-DQB1*04:04',
         'HLA-DQA1*04:01-DQB1*04:05', 'HLA-DQA1*04:01-DQB1*04:06', 'HLA-DQA1*04:01-DQB1*04:07', 'HLA-DQA1*04:01-DQB1*04:08',
         'HLA-DQA1*04:01-DQB1*05:01', 'HLA-DQA1*04:01-DQB1*05:02',
         'HLA-DQA1*04:01-DQB1*05:03', 'HLA-DQA1*04:01-DQB1*05:05', 'HLA-DQA1*04:01-DQB1*05:06', 'HLA-DQA1*04:01-DQB1*05:07',
         'HLA-DQA1*04:01-DQB1*05:08', 'HLA-DQA1*04:01-DQB1*05:09',
         'HLA-DQA1*04:01-DQB1*05:10', 'HLA-DQA1*04:01-DQB1*05:11', 'HLA-DQA1*04:01-DQB1*05:12', 'HLA-DQA1*04:01-DQB1*05:13',
         'HLA-DQA1*04:01-DQB1*05:14', 'HLA-DQA1*04:01-DQB1*06:01',
         'HLA-DQA1*04:01-DQB1*06:02', 'HLA-DQA1*04:01-DQB1*06:03', 'HLA-DQA1*04:01-DQB1*06:04', 'HLA-DQA1*04:01-DQB1*06:07',
         'HLA-DQA1*04:01-DQB1*06:08', 'HLA-DQA1*04:01-DQB1*06:09',
         'HLA-DQA1*04:01-DQB1*06:10', 'HLA-DQA1*04:01-DQB1*06:11', 'HLA-DQA1*04:01-DQB1*06:12', 'HLA-DQA1*04:01-DQB1*06:14',
         'HLA-DQA1*04:01-DQB1*06:15', 'HLA-DQA1*04:01-DQB1*06:16',
         'HLA-DQA1*04:01-DQB1*06:17', 'HLA-DQA1*04:01-DQB1*06:18', 'HLA-DQA1*04:01-DQB1*06:19', 'HLA-DQA1*04:01-DQB1*06:21',
         'HLA-DQA1*04:01-DQB1*06:22', 'HLA-DQA1*04:01-DQB1*06:23',
         'HLA-DQA1*04:01-DQB1*06:24', 'HLA-DQA1*04:01-DQB1*06:25', 'HLA-DQA1*04:01-DQB1*06:27', 'HLA-DQA1*04:01-DQB1*06:28',
         'HLA-DQA1*04:01-DQB1*06:29', 'HLA-DQA1*04:01-DQB1*06:30',
         'HLA-DQA1*04:01-DQB1*06:31', 'HLA-DQA1*04:01-DQB1*06:32', 'HLA-DQA1*04:01-DQB1*06:33', 'HLA-DQA1*04:01-DQB1*06:34',
         'HLA-DQA1*04:01-DQB1*06:35', 'HLA-DQA1*04:01-DQB1*06:36',
         'HLA-DQA1*04:01-DQB1*06:37', 'HLA-DQA1*04:01-DQB1*06:38', 'HLA-DQA1*04:01-DQB1*06:39', 'HLA-DQA1*04:01-DQB1*06:40',
         'HLA-DQA1*04:01-DQB1*06:41', 'HLA-DQA1*04:01-DQB1*06:42',
         'HLA-DQA1*04:01-DQB1*06:43', 'HLA-DQA1*04:01-DQB1*06:44', 'HLA-DQA1*04:02-DQB1*02:01', 'HLA-DQA1*04:02-DQB1*02:02',
         'HLA-DQA1*04:02-DQB1*02:03', 'HLA-DQA1*04:02-DQB1*02:04',
         'HLA-DQA1*04:02-DQB1*02:05', 'HLA-DQA1*04:02-DQB1*02:06', 'HLA-DQA1*04:02-DQB1*03:01', 'HLA-DQA1*04:02-DQB1*03:02',
         'HLA-DQA1*04:02-DQB1*03:03', 'HLA-DQA1*04:02-DQB1*03:04',
         'HLA-DQA1*04:02-DQB1*03:05', 'HLA-DQA1*04:02-DQB1*03:06', 'HLA-DQA1*04:02-DQB1*03:07', 'HLA-DQA1*04:02-DQB1*03:08',
         'HLA-DQA1*04:02-DQB1*03:09', 'HLA-DQA1*04:02-DQB1*03:10',
         'HLA-DQA1*04:02-DQB1*03:11', 'HLA-DQA1*04:02-DQB1*03:12', 'HLA-DQA1*04:02-DQB1*03:13', 'HLA-DQA1*04:02-DQB1*03:14',
         'HLA-DQA1*04:02-DQB1*03:15', 'HLA-DQA1*04:02-DQB1*03:16',
         'HLA-DQA1*04:02-DQB1*03:17', 'HLA-DQA1*04:02-DQB1*03:18', 'HLA-DQA1*04:02-DQB1*03:19', 'HLA-DQA1*04:02-DQB1*03:20',
         'HLA-DQA1*04:02-DQB1*03:21', 'HLA-DQA1*04:02-DQB1*03:22',
         'HLA-DQA1*04:02-DQB1*03:23', 'HLA-DQA1*04:02-DQB1*03:24', 'HLA-DQA1*04:02-DQB1*03:25', 'HLA-DQA1*04:02-DQB1*03:26',
         'HLA-DQA1*04:02-DQB1*03:27', 'HLA-DQA1*04:02-DQB1*03:28',
         'HLA-DQA1*04:02-DQB1*03:29', 'HLA-DQA1*04:02-DQB1*03:30', 'HLA-DQA1*04:02-DQB1*03:31', 'HLA-DQA1*04:02-DQB1*03:32',
         'HLA-DQA1*04:02-DQB1*03:33', 'HLA-DQA1*04:02-DQB1*03:34',
         'HLA-DQA1*04:02-DQB1*03:35', 'HLA-DQA1*04:02-DQB1*03:36', 'HLA-DQA1*04:02-DQB1*03:37', 'HLA-DQA1*04:02-DQB1*03:38',
         'HLA-DQA1*04:02-DQB1*04:01', 'HLA-DQA1*04:02-DQB1*04:02',
         'HLA-DQA1*04:02-DQB1*04:03', 'HLA-DQA1*04:02-DQB1*04:04', 'HLA-DQA1*04:02-DQB1*04:05', 'HLA-DQA1*04:02-DQB1*04:06',
         'HLA-DQA1*04:02-DQB1*04:07', 'HLA-DQA1*04:02-DQB1*04:08',
         'HLA-DQA1*04:02-DQB1*05:01', 'HLA-DQA1*04:02-DQB1*05:02', 'HLA-DQA1*04:02-DQB1*05:03', 'HLA-DQA1*04:02-DQB1*05:05',
         'HLA-DQA1*04:02-DQB1*05:06', 'HLA-DQA1*04:02-DQB1*05:07',
         'HLA-DQA1*04:02-DQB1*05:08', 'HLA-DQA1*04:02-DQB1*05:09', 'HLA-DQA1*04:02-DQB1*05:10', 'HLA-DQA1*04:02-DQB1*05:11',
         'HLA-DQA1*04:02-DQB1*05:12', 'HLA-DQA1*04:02-DQB1*05:13',
         'HLA-DQA1*04:02-DQB1*05:14', 'HLA-DQA1*04:02-DQB1*06:01', 'HLA-DQA1*04:02-DQB1*06:02', 'HLA-DQA1*04:02-DQB1*06:03',
         'HLA-DQA1*04:02-DQB1*06:04', 'HLA-DQA1*04:02-DQB1*06:07',
         'HLA-DQA1*04:02-DQB1*06:08', 'HLA-DQA1*04:02-DQB1*06:09', 'HLA-DQA1*04:02-DQB1*06:10', 'HLA-DQA1*04:02-DQB1*06:11',
         'HLA-DQA1*04:02-DQB1*06:12', 'HLA-DQA1*04:02-DQB1*06:14',
         'HLA-DQA1*04:02-DQB1*06:15', 'HLA-DQA1*04:02-DQB1*06:16', 'HLA-DQA1*04:02-DQB1*06:17', 'HLA-DQA1*04:02-DQB1*06:18',
         'HLA-DQA1*04:02-DQB1*06:19', 'HLA-DQA1*04:02-DQB1*06:21',
         'HLA-DQA1*04:02-DQB1*06:22', 'HLA-DQA1*04:02-DQB1*06:23', 'HLA-DQA1*04:02-DQB1*06:24', 'HLA-DQA1*04:02-DQB1*06:25',
         'HLA-DQA1*04:02-DQB1*06:27', 'HLA-DQA1*04:02-DQB1*06:28',
         'HLA-DQA1*04:02-DQB1*06:29', 'HLA-DQA1*04:02-DQB1*06:30', 'HLA-DQA1*04:02-DQB1*06:31', 'HLA-DQA1*04:02-DQB1*06:32',
         'HLA-DQA1*04:02-DQB1*06:33', 'HLA-DQA1*04:02-DQB1*06:34',
         'HLA-DQA1*04:02-DQB1*06:35', 'HLA-DQA1*04:02-DQB1*06:36', 'HLA-DQA1*04:02-DQB1*06:37', 'HLA-DQA1*04:02-DQB1*06:38',
         'HLA-DQA1*04:02-DQB1*06:39', 'HLA-DQA1*04:02-DQB1*06:40',
         'HLA-DQA1*04:02-DQB1*06:41', 'HLA-DQA1*04:02-DQB1*06:42', 'HLA-DQA1*04:02-DQB1*06:43', 'HLA-DQA1*04:02-DQB1*06:44',
         'HLA-DQA1*04:04-DQB1*02:01', 'HLA-DQA1*04:04-DQB1*02:02',
         'HLA-DQA1*04:04-DQB1*02:03', 'HLA-DQA1*04:04-DQB1*02:04', 'HLA-DQA1*04:04-DQB1*02:05', 'HLA-DQA1*04:04-DQB1*02:06',
         'HLA-DQA1*04:04-DQB1*03:01', 'HLA-DQA1*04:04-DQB1*03:02',
         'HLA-DQA1*04:04-DQB1*03:03', 'HLA-DQA1*04:04-DQB1*03:04', 'HLA-DQA1*04:04-DQB1*03:05', 'HLA-DQA1*04:04-DQB1*03:06',
         'HLA-DQA1*04:04-DQB1*03:07', 'HLA-DQA1*04:04-DQB1*03:08',
         'HLA-DQA1*04:04-DQB1*03:09', 'HLA-DQA1*04:04-DQB1*03:10', 'HLA-DQA1*04:04-DQB1*03:11', 'HLA-DQA1*04:04-DQB1*03:12',
         'HLA-DQA1*04:04-DQB1*03:13', 'HLA-DQA1*04:04-DQB1*03:14',
         'HLA-DQA1*04:04-DQB1*03:15', 'HLA-DQA1*04:04-DQB1*03:16', 'HLA-DQA1*04:04-DQB1*03:17', 'HLA-DQA1*04:04-DQB1*03:18',
         'HLA-DQA1*04:04-DQB1*03:19', 'HLA-DQA1*04:04-DQB1*03:20',
         'HLA-DQA1*04:04-DQB1*03:21', 'HLA-DQA1*04:04-DQB1*03:22', 'HLA-DQA1*04:04-DQB1*03:23', 'HLA-DQA1*04:04-DQB1*03:24',
         'HLA-DQA1*04:04-DQB1*03:25', 'HLA-DQA1*04:04-DQB1*03:26',
         'HLA-DQA1*04:04-DQB1*03:27', 'HLA-DQA1*04:04-DQB1*03:28', 'HLA-DQA1*04:04-DQB1*03:29', 'HLA-DQA1*04:04-DQB1*03:30',
         'HLA-DQA1*04:04-DQB1*03:31', 'HLA-DQA1*04:04-DQB1*03:32',
         'HLA-DQA1*04:04-DQB1*03:33', 'HLA-DQA1*04:04-DQB1*03:34', 'HLA-DQA1*04:04-DQB1*03:35', 'HLA-DQA1*04:04-DQB1*03:36',
         'HLA-DQA1*04:04-DQB1*03:37', 'HLA-DQA1*04:04-DQB1*03:38',
         'HLA-DQA1*04:04-DQB1*04:01', 'HLA-DQA1*04:04-DQB1*04:02', 'HLA-DQA1*04:04-DQB1*04:03', 'HLA-DQA1*04:04-DQB1*04:04',
         'HLA-DQA1*04:04-DQB1*04:05', 'HLA-DQA1*04:04-DQB1*04:06',
         'HLA-DQA1*04:04-DQB1*04:07', 'HLA-DQA1*04:04-DQB1*04:08', 'HLA-DQA1*04:04-DQB1*05:01', 'HLA-DQA1*04:04-DQB1*05:02',
         'HLA-DQA1*04:04-DQB1*05:03', 'HLA-DQA1*04:04-DQB1*05:05',
         'HLA-DQA1*04:04-DQB1*05:06', 'HLA-DQA1*04:04-DQB1*05:07', 'HLA-DQA1*04:04-DQB1*05:08', 'HLA-DQA1*04:04-DQB1*05:09',
         'HLA-DQA1*04:04-DQB1*05:10', 'HLA-DQA1*04:04-DQB1*05:11',
         'HLA-DQA1*04:04-DQB1*05:12', 'HLA-DQA1*04:04-DQB1*05:13', 'HLA-DQA1*04:04-DQB1*05:14', 'HLA-DQA1*04:04-DQB1*06:01',
         'HLA-DQA1*04:04-DQB1*06:02', 'HLA-DQA1*04:04-DQB1*06:03',
         'HLA-DQA1*04:04-DQB1*06:04', 'HLA-DQA1*04:04-DQB1*06:07', 'HLA-DQA1*04:04-DQB1*06:08', 'HLA-DQA1*04:04-DQB1*06:09',
         'HLA-DQA1*04:04-DQB1*06:10', 'HLA-DQA1*04:04-DQB1*06:11',
         'HLA-DQA1*04:04-DQB1*06:12', 'HLA-DQA1*04:04-DQB1*06:14', 'HLA-DQA1*04:04-DQB1*06:15', 'HLA-DQA1*04:04-DQB1*06:16',
         'HLA-DQA1*04:04-DQB1*06:17', 'HLA-DQA1*04:04-DQB1*06:18',
         'HLA-DQA1*04:04-DQB1*06:19', 'HLA-DQA1*04:04-DQB1*06:21', 'HLA-DQA1*04:04-DQB1*06:22', 'HLA-DQA1*04:04-DQB1*06:23',
         'HLA-DQA1*04:04-DQB1*06:24', 'HLA-DQA1*04:04-DQB1*06:25',
         'HLA-DQA1*04:04-DQB1*06:27', 'HLA-DQA1*04:04-DQB1*06:28', 'HLA-DQA1*04:04-DQB1*06:29', 'HLA-DQA1*04:04-DQB1*06:30',
         'HLA-DQA1*04:04-DQB1*06:31', 'HLA-DQA1*04:04-DQB1*06:32',
         'HLA-DQA1*04:04-DQB1*06:33', 'HLA-DQA1*04:04-DQB1*06:34', 'HLA-DQA1*04:04-DQB1*06:35', 'HLA-DQA1*04:04-DQB1*06:36',
         'HLA-DQA1*04:04-DQB1*06:37', 'HLA-DQA1*04:04-DQB1*06:38',
         'HLA-DQA1*04:04-DQB1*06:39', 'HLA-DQA1*04:04-DQB1*06:40', 'HLA-DQA1*04:04-DQB1*06:41', 'HLA-DQA1*04:04-DQB1*06:42',
         'HLA-DQA1*04:04-DQB1*06:43', 'HLA-DQA1*04:04-DQB1*06:44',
         'HLA-DQA1*05:01-DQB1*02:01', 'HLA-DQA1*05:01-DQB1*02:02', 'HLA-DQA1*05:01-DQB1*02:03', 'HLA-DQA1*05:01-DQB1*02:04',
         'HLA-DQA1*05:01-DQB1*02:05', 'HLA-DQA1*05:01-DQB1*02:06',
         'HLA-DQA1*05:01-DQB1*03:01', 'HLA-DQA1*05:01-DQB1*03:02', 'HLA-DQA1*05:01-DQB1*03:03', 'HLA-DQA1*05:01-DQB1*03:04',
         'HLA-DQA1*05:01-DQB1*03:05', 'HLA-DQA1*05:01-DQB1*03:06',
         'HLA-DQA1*05:01-DQB1*03:07', 'HLA-DQA1*05:01-DQB1*03:08', 'HLA-DQA1*05:01-DQB1*03:09', 'HLA-DQA1*05:01-DQB1*03:10',
         'HLA-DQA1*05:01-DQB1*03:11', 'HLA-DQA1*05:01-DQB1*03:12',
         'HLA-DQA1*05:01-DQB1*03:13', 'HLA-DQA1*05:01-DQB1*03:14', 'HLA-DQA1*05:01-DQB1*03:15', 'HLA-DQA1*05:01-DQB1*03:16',
         'HLA-DQA1*05:01-DQB1*03:17', 'HLA-DQA1*05:01-DQB1*03:18',
         'HLA-DQA1*05:01-DQB1*03:19', 'HLA-DQA1*05:01-DQB1*03:20', 'HLA-DQA1*05:01-DQB1*03:21', 'HLA-DQA1*05:01-DQB1*03:22',
         'HLA-DQA1*05:01-DQB1*03:23', 'HLA-DQA1*05:01-DQB1*03:24',
         'HLA-DQA1*05:01-DQB1*03:25', 'HLA-DQA1*05:01-DQB1*03:26', 'HLA-DQA1*05:01-DQB1*03:27', 'HLA-DQA1*05:01-DQB1*03:28',
         'HLA-DQA1*05:01-DQB1*03:29', 'HLA-DQA1*05:01-DQB1*03:30',
         'HLA-DQA1*05:01-DQB1*03:31', 'HLA-DQA1*05:01-DQB1*03:32', 'HLA-DQA1*05:01-DQB1*03:33', 'HLA-DQA1*05:01-DQB1*03:34',
         'HLA-DQA1*05:01-DQB1*03:35', 'HLA-DQA1*05:01-DQB1*03:36',
         'HLA-DQA1*05:01-DQB1*03:37', 'HLA-DQA1*05:01-DQB1*03:38', 'HLA-DQA1*05:01-DQB1*04:01', 'HLA-DQA1*05:01-DQB1*04:02',
         'HLA-DQA1*05:01-DQB1*04:03', 'HLA-DQA1*05:01-DQB1*04:04',
         'HLA-DQA1*05:01-DQB1*04:05', 'HLA-DQA1*05:01-DQB1*04:06', 'HLA-DQA1*05:01-DQB1*04:07', 'HLA-DQA1*05:01-DQB1*04:08',
         'HLA-DQA1*05:01-DQB1*05:01', 'HLA-DQA1*05:01-DQB1*05:02',
         'HLA-DQA1*05:01-DQB1*05:03', 'HLA-DQA1*05:01-DQB1*05:05', 'HLA-DQA1*05:01-DQB1*05:06', 'HLA-DQA1*05:01-DQB1*05:07',
         'HLA-DQA1*05:01-DQB1*05:08', 'HLA-DQA1*05:01-DQB1*05:09',
         'HLA-DQA1*05:01-DQB1*05:10', 'HLA-DQA1*05:01-DQB1*05:11', 'HLA-DQA1*05:01-DQB1*05:12', 'HLA-DQA1*05:01-DQB1*05:13',
         'HLA-DQA1*05:01-DQB1*05:14', 'HLA-DQA1*05:01-DQB1*06:01',
         'HLA-DQA1*05:01-DQB1*06:02', 'HLA-DQA1*05:01-DQB1*06:03', 'HLA-DQA1*05:01-DQB1*06:04', 'HLA-DQA1*05:01-DQB1*06:07',
         'HLA-DQA1*05:01-DQB1*06:08', 'HLA-DQA1*05:01-DQB1*06:09',
         'HLA-DQA1*05:01-DQB1*06:10', 'HLA-DQA1*05:01-DQB1*06:11', 'HLA-DQA1*05:01-DQB1*06:12', 'HLA-DQA1*05:01-DQB1*06:14',
         'HLA-DQA1*05:01-DQB1*06:15', 'HLA-DQA1*05:01-DQB1*06:16',
         'HLA-DQA1*05:01-DQB1*06:17', 'HLA-DQA1*05:01-DQB1*06:18', 'HLA-DQA1*05:01-DQB1*06:19', 'HLA-DQA1*05:01-DQB1*06:21',
         'HLA-DQA1*05:01-DQB1*06:22', 'HLA-DQA1*05:01-DQB1*06:23',
         'HLA-DQA1*05:01-DQB1*06:24', 'HLA-DQA1*05:01-DQB1*06:25', 'HLA-DQA1*05:01-DQB1*06:27', 'HLA-DQA1*05:01-DQB1*06:28',
         'HLA-DQA1*05:01-DQB1*06:29', 'HLA-DQA1*05:01-DQB1*06:30',
         'HLA-DQA1*05:01-DQB1*06:31', 'HLA-DQA1*05:01-DQB1*06:32', 'HLA-DQA1*05:01-DQB1*06:33', 'HLA-DQA1*05:01-DQB1*06:34',
         'HLA-DQA1*05:01-DQB1*06:35', 'HLA-DQA1*05:01-DQB1*06:36',
         'HLA-DQA1*05:01-DQB1*06:37', 'HLA-DQA1*05:01-DQB1*06:38', 'HLA-DQA1*05:01-DQB1*06:39', 'HLA-DQA1*05:01-DQB1*06:40',
         'HLA-DQA1*05:01-DQB1*06:41', 'HLA-DQA1*05:01-DQB1*06:42',
         'HLA-DQA1*05:01-DQB1*06:43', 'HLA-DQA1*05:01-DQB1*06:44', 'HLA-DQA1*05:03-DQB1*02:01', 'HLA-DQA1*05:03-DQB1*02:02',
         'HLA-DQA1*05:03-DQB1*02:03', 'HLA-DQA1*05:03-DQB1*02:04',
         'HLA-DQA1*05:03-DQB1*02:05', 'HLA-DQA1*05:03-DQB1*02:06', 'HLA-DQA1*05:03-DQB1*03:01', 'HLA-DQA1*05:03-DQB1*03:02',
         'HLA-DQA1*05:03-DQB1*03:03', 'HLA-DQA1*05:03-DQB1*03:04',
         'HLA-DQA1*05:03-DQB1*03:05', 'HLA-DQA1*05:03-DQB1*03:06', 'HLA-DQA1*05:03-DQB1*03:07', 'HLA-DQA1*05:03-DQB1*03:08',
         'HLA-DQA1*05:03-DQB1*03:09', 'HLA-DQA1*05:03-DQB1*03:10',
         'HLA-DQA1*05:03-DQB1*03:11', 'HLA-DQA1*05:03-DQB1*03:12', 'HLA-DQA1*05:03-DQB1*03:13', 'HLA-DQA1*05:03-DQB1*03:14',
         'HLA-DQA1*05:03-DQB1*03:15', 'HLA-DQA1*05:03-DQB1*03:16',
         'HLA-DQA1*05:03-DQB1*03:17', 'HLA-DQA1*05:03-DQB1*03:18', 'HLA-DQA1*05:03-DQB1*03:19', 'HLA-DQA1*05:03-DQB1*03:20',
         'HLA-DQA1*05:03-DQB1*03:21', 'HLA-DQA1*05:03-DQB1*03:22',
         'HLA-DQA1*05:03-DQB1*03:23', 'HLA-DQA1*05:03-DQB1*03:24', 'HLA-DQA1*05:03-DQB1*03:25', 'HLA-DQA1*05:03-DQB1*03:26',
         'HLA-DQA1*05:03-DQB1*03:27', 'HLA-DQA1*05:03-DQB1*03:28',
         'HLA-DQA1*05:03-DQB1*03:29', 'HLA-DQA1*05:03-DQB1*03:30', 'HLA-DQA1*05:03-DQB1*03:31', 'HLA-DQA1*05:03-DQB1*03:32',
         'HLA-DQA1*05:03-DQB1*03:33', 'HLA-DQA1*05:03-DQB1*03:34',
         'HLA-DQA1*05:03-DQB1*03:35', 'HLA-DQA1*05:03-DQB1*03:36', 'HLA-DQA1*05:03-DQB1*03:37', 'HLA-DQA1*05:03-DQB1*03:38',
         'HLA-DQA1*05:03-DQB1*04:01', 'HLA-DQA1*05:03-DQB1*04:02',
         'HLA-DQA1*05:03-DQB1*04:03', 'HLA-DQA1*05:03-DQB1*04:04', 'HLA-DQA1*05:03-DQB1*04:05', 'HLA-DQA1*05:03-DQB1*04:06',
         'HLA-DQA1*05:03-DQB1*04:07', 'HLA-DQA1*05:03-DQB1*04:08',
         'HLA-DQA1*05:03-DQB1*05:01', 'HLA-DQA1*05:03-DQB1*05:02', 'HLA-DQA1*05:03-DQB1*05:03', 'HLA-DQA1*05:03-DQB1*05:05',
         'HLA-DQA1*05:03-DQB1*05:06', 'HLA-DQA1*05:03-DQB1*05:07',
         'HLA-DQA1*05:03-DQB1*05:08', 'HLA-DQA1*05:03-DQB1*05:09', 'HLA-DQA1*05:03-DQB1*05:10', 'HLA-DQA1*05:03-DQB1*05:11',
         'HLA-DQA1*05:03-DQB1*05:12', 'HLA-DQA1*05:03-DQB1*05:13',
         'HLA-DQA1*05:03-DQB1*05:14', 'HLA-DQA1*05:03-DQB1*06:01', 'HLA-DQA1*05:03-DQB1*06:02', 'HLA-DQA1*05:03-DQB1*06:03',
         'HLA-DQA1*05:03-DQB1*06:04', 'HLA-DQA1*05:03-DQB1*06:07',
         'HLA-DQA1*05:03-DQB1*06:08', 'HLA-DQA1*05:03-DQB1*06:09', 'HLA-DQA1*05:03-DQB1*06:10', 'HLA-DQA1*05:03-DQB1*06:11',
         'HLA-DQA1*05:03-DQB1*06:12', 'HLA-DQA1*05:03-DQB1*06:14',
         'HLA-DQA1*05:03-DQB1*06:15', 'HLA-DQA1*05:03-DQB1*06:16', 'HLA-DQA1*05:03-DQB1*06:17', 'HLA-DQA1*05:03-DQB1*06:18',
         'HLA-DQA1*05:03-DQB1*06:19', 'HLA-DQA1*05:03-DQB1*06:21',
         'HLA-DQA1*05:03-DQB1*06:22', 'HLA-DQA1*05:03-DQB1*06:23', 'HLA-DQA1*05:03-DQB1*06:24', 'HLA-DQA1*05:03-DQB1*06:25',
         'HLA-DQA1*05:03-DQB1*06:27', 'HLA-DQA1*05:03-DQB1*06:28',
         'HLA-DQA1*05:03-DQB1*06:29', 'HLA-DQA1*05:03-DQB1*06:30', 'HLA-DQA1*05:03-DQB1*06:31', 'HLA-DQA1*05:03-DQB1*06:32',
         'HLA-DQA1*05:03-DQB1*06:33', 'HLA-DQA1*05:03-DQB1*06:34',
         'HLA-DQA1*05:03-DQB1*06:35', 'HLA-DQA1*05:03-DQB1*06:36', 'HLA-DQA1*05:03-DQB1*06:37', 'HLA-DQA1*05:03-DQB1*06:38',
         'HLA-DQA1*05:03-DQB1*06:39', 'HLA-DQA1*05:03-DQB1*06:40',
         'HLA-DQA1*05:03-DQB1*06:41', 'HLA-DQA1*05:03-DQB1*06:42', 'HLA-DQA1*05:03-DQB1*06:43', 'HLA-DQA1*05:03-DQB1*06:44',
         'HLA-DQA1*05:04-DQB1*02:01', 'HLA-DQA1*05:04-DQB1*02:02',
         'HLA-DQA1*05:04-DQB1*02:03', 'HLA-DQA1*05:04-DQB1*02:04', 'HLA-DQA1*05:04-DQB1*02:05', 'HLA-DQA1*05:04-DQB1*02:06',
         'HLA-DQA1*05:04-DQB1*03:01', 'HLA-DQA1*05:04-DQB1*03:02',
         'HLA-DQA1*05:04-DQB1*03:03', 'HLA-DQA1*05:04-DQB1*03:04', 'HLA-DQA1*05:04-DQB1*03:05', 'HLA-DQA1*05:04-DQB1*03:06',
         'HLA-DQA1*05:04-DQB1*03:07', 'HLA-DQA1*05:04-DQB1*03:08',
         'HLA-DQA1*05:04-DQB1*03:09', 'HLA-DQA1*05:04-DQB1*03:10', 'HLA-DQA1*05:04-DQB1*03:11', 'HLA-DQA1*05:04-DQB1*03:12',
         'HLA-DQA1*05:04-DQB1*03:13', 'HLA-DQA1*05:04-DQB1*03:14',
         'HLA-DQA1*05:04-DQB1*03:15', 'HLA-DQA1*05:04-DQB1*03:16', 'HLA-DQA1*05:04-DQB1*03:17', 'HLA-DQA1*05:04-DQB1*03:18',
         'HLA-DQA1*05:04-DQB1*03:19', 'HLA-DQA1*05:04-DQB1*03:20',
         'HLA-DQA1*05:04-DQB1*03:21', 'HLA-DQA1*05:04-DQB1*03:22', 'HLA-DQA1*05:04-DQB1*03:23', 'HLA-DQA1*05:04-DQB1*03:24',
         'HLA-DQA1*05:04-DQB1*03:25', 'HLA-DQA1*05:04-DQB1*03:26',
         'HLA-DQA1*05:04-DQB1*03:27', 'HLA-DQA1*05:04-DQB1*03:28', 'HLA-DQA1*05:04-DQB1*03:29', 'HLA-DQA1*05:04-DQB1*03:30',
         'HLA-DQA1*05:04-DQB1*03:31', 'HLA-DQA1*05:04-DQB1*03:32',
         'HLA-DQA1*05:04-DQB1*03:33', 'HLA-DQA1*05:04-DQB1*03:34', 'HLA-DQA1*05:04-DQB1*03:35', 'HLA-DQA1*05:04-DQB1*03:36',
         'HLA-DQA1*05:04-DQB1*03:37', 'HLA-DQA1*05:04-DQB1*03:38',
         'HLA-DQA1*05:04-DQB1*04:01', 'HLA-DQA1*05:04-DQB1*04:02', 'HLA-DQA1*05:04-DQB1*04:03', 'HLA-DQA1*05:04-DQB1*04:04',
         'HLA-DQA1*05:04-DQB1*04:05', 'HLA-DQA1*05:04-DQB1*04:06',
         'HLA-DQA1*05:04-DQB1*04:07', 'HLA-DQA1*05:04-DQB1*04:08', 'HLA-DQA1*05:04-DQB1*05:01', 'HLA-DQA1*05:04-DQB1*05:02',
         'HLA-DQA1*05:04-DQB1*05:03', 'HLA-DQA1*05:04-DQB1*05:05',
         'HLA-DQA1*05:04-DQB1*05:06', 'HLA-DQA1*05:04-DQB1*05:07', 'HLA-DQA1*05:04-DQB1*05:08', 'HLA-DQA1*05:04-DQB1*05:09',
         'HLA-DQA1*05:04-DQB1*05:10', 'HLA-DQA1*05:04-DQB1*05:11',
         'HLA-DQA1*05:04-DQB1*05:12', 'HLA-DQA1*05:04-DQB1*05:13', 'HLA-DQA1*05:04-DQB1*05:14', 'HLA-DQA1*05:04-DQB1*06:01',
         'HLA-DQA1*05:04-DQB1*06:02', 'HLA-DQA1*05:04-DQB1*06:03',
         'HLA-DQA1*05:04-DQB1*06:04', 'HLA-DQA1*05:04-DQB1*06:07', 'HLA-DQA1*05:04-DQB1*06:08', 'HLA-DQA1*05:04-DQB1*06:09',
         'HLA-DQA1*05:04-DQB1*06:10', 'HLA-DQA1*05:04-DQB1*06:11',
         'HLA-DQA1*05:04-DQB1*06:12', 'HLA-DQA1*05:04-DQB1*06:14', 'HLA-DQA1*05:04-DQB1*06:15', 'HLA-DQA1*05:04-DQB1*06:16',
         'HLA-DQA1*05:04-DQB1*06:17', 'HLA-DQA1*05:04-DQB1*06:18',
         'HLA-DQA1*05:04-DQB1*06:19', 'HLA-DQA1*05:04-DQB1*06:21', 'HLA-DQA1*05:04-DQB1*06:22', 'HLA-DQA1*05:04-DQB1*06:23',
         'HLA-DQA1*05:04-DQB1*06:24', 'HLA-DQA1*05:04-DQB1*06:25',
         'HLA-DQA1*05:04-DQB1*06:27', 'HLA-DQA1*05:04-DQB1*06:28', 'HLA-DQA1*05:04-DQB1*06:29', 'HLA-DQA1*05:04-DQB1*06:30',
         'HLA-DQA1*05:04-DQB1*06:31', 'HLA-DQA1*05:04-DQB1*06:32',
         'HLA-DQA1*05:04-DQB1*06:33', 'HLA-DQA1*05:04-DQB1*06:34', 'HLA-DQA1*05:04-DQB1*06:35', 'HLA-DQA1*05:04-DQB1*06:36',
         'HLA-DQA1*05:04-DQB1*06:37', 'HLA-DQA1*05:04-DQB1*06:38',
         'HLA-DQA1*05:04-DQB1*06:39', 'HLA-DQA1*05:04-DQB1*06:40', 'HLA-DQA1*05:04-DQB1*06:41', 'HLA-DQA1*05:04-DQB1*06:42',
         'HLA-DQA1*05:04-DQB1*06:43', 'HLA-DQA1*05:04-DQB1*06:44',
         'HLA-DQA1*05:05-DQB1*02:01', 'HLA-DQA1*05:05-DQB1*02:02', 'HLA-DQA1*05:05-DQB1*02:03', 'HLA-DQA1*05:05-DQB1*02:04',
         'HLA-DQA1*05:05-DQB1*02:05', 'HLA-DQA1*05:05-DQB1*02:06',
         'HLA-DQA1*05:05-DQB1*03:01', 'HLA-DQA1*05:05-DQB1*03:02', 'HLA-DQA1*05:05-DQB1*03:03', 'HLA-DQA1*05:05-DQB1*03:04',
         'HLA-DQA1*05:05-DQB1*03:05', 'HLA-DQA1*05:05-DQB1*03:06',
         'HLA-DQA1*05:05-DQB1*03:07', 'HLA-DQA1*05:05-DQB1*03:08', 'HLA-DQA1*05:05-DQB1*03:09', 'HLA-DQA1*05:05-DQB1*03:10',
         'HLA-DQA1*05:05-DQB1*03:11', 'HLA-DQA1*05:05-DQB1*03:12',
         'HLA-DQA1*05:05-DQB1*03:13', 'HLA-DQA1*05:05-DQB1*03:14', 'HLA-DQA1*05:05-DQB1*03:15', 'HLA-DQA1*05:05-DQB1*03:16',
         'HLA-DQA1*05:05-DQB1*03:17', 'HLA-DQA1*05:05-DQB1*03:18',
         'HLA-DQA1*05:05-DQB1*03:19', 'HLA-DQA1*05:05-DQB1*03:20', 'HLA-DQA1*05:05-DQB1*03:21', 'HLA-DQA1*05:05-DQB1*03:22',
         'HLA-DQA1*05:05-DQB1*03:23', 'HLA-DQA1*05:05-DQB1*03:24',
         'HLA-DQA1*05:05-DQB1*03:25', 'HLA-DQA1*05:05-DQB1*03:26', 'HLA-DQA1*05:05-DQB1*03:27', 'HLA-DQA1*05:05-DQB1*03:28',
         'HLA-DQA1*05:05-DQB1*03:29', 'HLA-DQA1*05:05-DQB1*03:30',
         'HLA-DQA1*05:05-DQB1*03:31', 'HLA-DQA1*05:05-DQB1*03:32', 'HLA-DQA1*05:05-DQB1*03:33', 'HLA-DQA1*05:05-DQB1*03:34',
         'HLA-DQA1*05:05-DQB1*03:35', 'HLA-DQA1*05:05-DQB1*03:36',
         'HLA-DQA1*05:05-DQB1*03:37', 'HLA-DQA1*05:05-DQB1*03:38', 'HLA-DQA1*05:05-DQB1*04:01', 'HLA-DQA1*05:05-DQB1*04:02',
         'HLA-DQA1*05:05-DQB1*04:03', 'HLA-DQA1*05:05-DQB1*04:04',
         'HLA-DQA1*05:05-DQB1*04:05', 'HLA-DQA1*05:05-DQB1*04:06', 'HLA-DQA1*05:05-DQB1*04:07', 'HLA-DQA1*05:05-DQB1*04:08',
         'HLA-DQA1*05:05-DQB1*05:01', 'HLA-DQA1*05:05-DQB1*05:02',
         'HLA-DQA1*05:05-DQB1*05:03', 'HLA-DQA1*05:05-DQB1*05:05', 'HLA-DQA1*05:05-DQB1*05:06', 'HLA-DQA1*05:05-DQB1*05:07',
         'HLA-DQA1*05:05-DQB1*05:08', 'HLA-DQA1*05:05-DQB1*05:09',
         'HLA-DQA1*05:05-DQB1*05:10', 'HLA-DQA1*05:05-DQB1*05:11', 'HLA-DQA1*05:05-DQB1*05:12', 'HLA-DQA1*05:05-DQB1*05:13',
         'HLA-DQA1*05:05-DQB1*05:14', 'HLA-DQA1*05:05-DQB1*06:01',
         'HLA-DQA1*05:05-DQB1*06:02', 'HLA-DQA1*05:05-DQB1*06:03', 'HLA-DQA1*05:05-DQB1*06:04', 'HLA-DQA1*05:05-DQB1*06:07',
         'HLA-DQA1*05:05-DQB1*06:08', 'HLA-DQA1*05:05-DQB1*06:09',
         'HLA-DQA1*05:05-DQB1*06:10', 'HLA-DQA1*05:05-DQB1*06:11', 'HLA-DQA1*05:05-DQB1*06:12', 'HLA-DQA1*05:05-DQB1*06:14',
         'HLA-DQA1*05:05-DQB1*06:15', 'HLA-DQA1*05:05-DQB1*06:16',
         'HLA-DQA1*05:05-DQB1*06:17', 'HLA-DQA1*05:05-DQB1*06:18', 'HLA-DQA1*05:05-DQB1*06:19', 'HLA-DQA1*05:05-DQB1*06:21',
         'HLA-DQA1*05:05-DQB1*06:22', 'HLA-DQA1*05:05-DQB1*06:23',
         'HLA-DQA1*05:05-DQB1*06:24', 'HLA-DQA1*05:05-DQB1*06:25', 'HLA-DQA1*05:05-DQB1*06:27', 'HLA-DQA1*05:05-DQB1*06:28',
         'HLA-DQA1*05:05-DQB1*06:29', 'HLA-DQA1*05:05-DQB1*06:30',
         'HLA-DQA1*05:05-DQB1*06:31', 'HLA-DQA1*05:05-DQB1*06:32', 'HLA-DQA1*05:05-DQB1*06:33', 'HLA-DQA1*05:05-DQB1*06:34',
         'HLA-DQA1*05:05-DQB1*06:35', 'HLA-DQA1*05:05-DQB1*06:36',
         'HLA-DQA1*05:05-DQB1*06:37', 'HLA-DQA1*05:05-DQB1*06:38', 'HLA-DQA1*05:05-DQB1*06:39', 'HLA-DQA1*05:05-DQB1*06:40',
         'HLA-DQA1*05:05-DQB1*06:41', 'HLA-DQA1*05:05-DQB1*06:42',
         'HLA-DQA1*05:05-DQB1*06:43', 'HLA-DQA1*05:05-DQB1*06:44', 'HLA-DQA1*05:06-DQB1*02:01', 'HLA-DQA1*05:06-DQB1*02:02',
         'HLA-DQA1*05:06-DQB1*02:03', 'HLA-DQA1*05:06-DQB1*02:04',
         'HLA-DQA1*05:06-DQB1*02:05', 'HLA-DQA1*05:06-DQB1*02:06', 'HLA-DQA1*05:06-DQB1*03:01', 'HLA-DQA1*05:06-DQB1*03:02',
         'HLA-DQA1*05:06-DQB1*03:03', 'HLA-DQA1*05:06-DQB1*03:04',
         'HLA-DQA1*05:06-DQB1*03:05', 'HLA-DQA1*05:06-DQB1*03:06', 'HLA-DQA1*05:06-DQB1*03:07', 'HLA-DQA1*05:06-DQB1*03:08',
         'HLA-DQA1*05:06-DQB1*03:09', 'HLA-DQA1*05:06-DQB1*03:10',
         'HLA-DQA1*05:06-DQB1*03:11', 'HLA-DQA1*05:06-DQB1*03:12', 'HLA-DQA1*05:06-DQB1*03:13', 'HLA-DQA1*05:06-DQB1*03:14',
         'HLA-DQA1*05:06-DQB1*03:15', 'HLA-DQA1*05:06-DQB1*03:16',
         'HLA-DQA1*05:06-DQB1*03:17', 'HLA-DQA1*05:06-DQB1*03:18', 'HLA-DQA1*05:06-DQB1*03:19', 'HLA-DQA1*05:06-DQB1*03:20',
         'HLA-DQA1*05:06-DQB1*03:21', 'HLA-DQA1*05:06-DQB1*03:22',
         'HLA-DQA1*05:06-DQB1*03:23', 'HLA-DQA1*05:06-DQB1*03:24', 'HLA-DQA1*05:06-DQB1*03:25', 'HLA-DQA1*05:06-DQB1*03:26',
         'HLA-DQA1*05:06-DQB1*03:27', 'HLA-DQA1*05:06-DQB1*03:28',
         'HLA-DQA1*05:06-DQB1*03:29', 'HLA-DQA1*05:06-DQB1*03:30', 'HLA-DQA1*05:06-DQB1*03:31', 'HLA-DQA1*05:06-DQB1*03:32',
         'HLA-DQA1*05:06-DQB1*03:33', 'HLA-DQA1*05:06-DQB1*03:34',
         'HLA-DQA1*05:06-DQB1*03:35', 'HLA-DQA1*05:06-DQB1*03:36', 'HLA-DQA1*05:06-DQB1*03:37', 'HLA-DQA1*05:06-DQB1*03:38',
         'HLA-DQA1*05:06-DQB1*04:01', 'HLA-DQA1*05:06-DQB1*04:02',
         'HLA-DQA1*05:06-DQB1*04:03', 'HLA-DQA1*05:06-DQB1*04:04', 'HLA-DQA1*05:06-DQB1*04:05', 'HLA-DQA1*05:06-DQB1*04:06',
         'HLA-DQA1*05:06-DQB1*04:07', 'HLA-DQA1*05:06-DQB1*04:08',
         'HLA-DQA1*05:06-DQB1*05:01', 'HLA-DQA1*05:06-DQB1*05:02', 'HLA-DQA1*05:06-DQB1*05:03', 'HLA-DQA1*05:06-DQB1*05:05',
         'HLA-DQA1*05:06-DQB1*05:06', 'HLA-DQA1*05:06-DQB1*05:07',
         'HLA-DQA1*05:06-DQB1*05:08', 'HLA-DQA1*05:06-DQB1*05:09', 'HLA-DQA1*05:06-DQB1*05:10', 'HLA-DQA1*05:06-DQB1*05:11',
         'HLA-DQA1*05:06-DQB1*05:12', 'HLA-DQA1*05:06-DQB1*05:13',
         'HLA-DQA1*05:06-DQB1*05:14', 'HLA-DQA1*05:06-DQB1*06:01', 'HLA-DQA1*05:06-DQB1*06:02', 'HLA-DQA1*05:06-DQB1*06:03',
         'HLA-DQA1*05:06-DQB1*06:04', 'HLA-DQA1*05:06-DQB1*06:07',
         'HLA-DQA1*05:06-DQB1*06:08', 'HLA-DQA1*05:06-DQB1*06:09', 'HLA-DQA1*05:06-DQB1*06:10', 'HLA-DQA1*05:06-DQB1*06:11',
         'HLA-DQA1*05:06-DQB1*06:12', 'HLA-DQA1*05:06-DQB1*06:14',
         'HLA-DQA1*05:06-DQB1*06:15', 'HLA-DQA1*05:06-DQB1*06:16', 'HLA-DQA1*05:06-DQB1*06:17', 'HLA-DQA1*05:06-DQB1*06:18',
         'HLA-DQA1*05:06-DQB1*06:19', 'HLA-DQA1*05:06-DQB1*06:21',
         'HLA-DQA1*05:06-DQB1*06:22', 'HLA-DQA1*05:06-DQB1*06:23', 'HLA-DQA1*05:06-DQB1*06:24', 'HLA-DQA1*05:06-DQB1*06:25',
         'HLA-DQA1*05:06-DQB1*06:27', 'HLA-DQA1*05:06-DQB1*06:28',
         'HLA-DQA1*05:06-DQB1*06:29', 'HLA-DQA1*05:06-DQB1*06:30', 'HLA-DQA1*05:06-DQB1*06:31', 'HLA-DQA1*05:06-DQB1*06:32',
         'HLA-DQA1*05:06-DQB1*06:33', 'HLA-DQA1*05:06-DQB1*06:34',
         'HLA-DQA1*05:06-DQB1*06:35', 'HLA-DQA1*05:06-DQB1*06:36', 'HLA-DQA1*05:06-DQB1*06:37', 'HLA-DQA1*05:06-DQB1*06:38',
         'HLA-DQA1*05:06-DQB1*06:39', 'HLA-DQA1*05:06-DQB1*06:40',
         'HLA-DQA1*05:06-DQB1*06:41', 'HLA-DQA1*05:06-DQB1*06:42', 'HLA-DQA1*05:06-DQB1*06:43', 'HLA-DQA1*05:06-DQB1*06:44',
         'HLA-DQA1*05:07-DQB1*02:01', 'HLA-DQA1*05:07-DQB1*02:02',
         'HLA-DQA1*05:07-DQB1*02:03', 'HLA-DQA1*05:07-DQB1*02:04', 'HLA-DQA1*05:07-DQB1*02:05', 'HLA-DQA1*05:07-DQB1*02:06',
         'HLA-DQA1*05:07-DQB1*03:01', 'HLA-DQA1*05:07-DQB1*03:02',
         'HLA-DQA1*05:07-DQB1*03:03', 'HLA-DQA1*05:07-DQB1*03:04', 'HLA-DQA1*05:07-DQB1*03:05', 'HLA-DQA1*05:07-DQB1*03:06',
         'HLA-DQA1*05:07-DQB1*03:07', 'HLA-DQA1*05:07-DQB1*03:08',
         'HLA-DQA1*05:07-DQB1*03:09', 'HLA-DQA1*05:07-DQB1*03:10', 'HLA-DQA1*05:07-DQB1*03:11', 'HLA-DQA1*05:07-DQB1*03:12',
         'HLA-DQA1*05:07-DQB1*03:13', 'HLA-DQA1*05:07-DQB1*03:14',
         'HLA-DQA1*05:07-DQB1*03:15', 'HLA-DQA1*05:07-DQB1*03:16', 'HLA-DQA1*05:07-DQB1*03:17', 'HLA-DQA1*05:07-DQB1*03:18',
         'HLA-DQA1*05:07-DQB1*03:19', 'HLA-DQA1*05:07-DQB1*03:20',
         'HLA-DQA1*05:07-DQB1*03:21', 'HLA-DQA1*05:07-DQB1*03:22', 'HLA-DQA1*05:07-DQB1*03:23', 'HLA-DQA1*05:07-DQB1*03:24',
         'HLA-DQA1*05:07-DQB1*03:25', 'HLA-DQA1*05:07-DQB1*03:26',
         'HLA-DQA1*05:07-DQB1*03:27', 'HLA-DQA1*05:07-DQB1*03:28', 'HLA-DQA1*05:07-DQB1*03:29', 'HLA-DQA1*05:07-DQB1*03:30',
         'HLA-DQA1*05:07-DQB1*03:31', 'HLA-DQA1*05:07-DQB1*03:32',
         'HLA-DQA1*05:07-DQB1*03:33', 'HLA-DQA1*05:07-DQB1*03:34', 'HLA-DQA1*05:07-DQB1*03:35', 'HLA-DQA1*05:07-DQB1*03:36',
         'HLA-DQA1*05:07-DQB1*03:37', 'HLA-DQA1*05:07-DQB1*03:38',
         'HLA-DQA1*05:07-DQB1*04:01', 'HLA-DQA1*05:07-DQB1*04:02', 'HLA-DQA1*05:07-DQB1*04:03', 'HLA-DQA1*05:07-DQB1*04:04',
         'HLA-DQA1*05:07-DQB1*04:05', 'HLA-DQA1*05:07-DQB1*04:06',
         'HLA-DQA1*05:07-DQB1*04:07', 'HLA-DQA1*05:07-DQB1*04:08', 'HLA-DQA1*05:07-DQB1*05:01', 'HLA-DQA1*05:07-DQB1*05:02',
         'HLA-DQA1*05:07-DQB1*05:03', 'HLA-DQA1*05:07-DQB1*05:05',
         'HLA-DQA1*05:07-DQB1*05:06', 'HLA-DQA1*05:07-DQB1*05:07', 'HLA-DQA1*05:07-DQB1*05:08', 'HLA-DQA1*05:07-DQB1*05:09',
         'HLA-DQA1*05:07-DQB1*05:10', 'HLA-DQA1*05:07-DQB1*05:11',
         'HLA-DQA1*05:07-DQB1*05:12', 'HLA-DQA1*05:07-DQB1*05:13', 'HLA-DQA1*05:07-DQB1*05:14', 'HLA-DQA1*05:07-DQB1*06:01',
         'HLA-DQA1*05:07-DQB1*06:02', 'HLA-DQA1*05:07-DQB1*06:03',
         'HLA-DQA1*05:07-DQB1*06:04', 'HLA-DQA1*05:07-DQB1*06:07', 'HLA-DQA1*05:07-DQB1*06:08', 'HLA-DQA1*05:07-DQB1*06:09',
         'HLA-DQA1*05:07-DQB1*06:10', 'HLA-DQA1*05:07-DQB1*06:11',
         'HLA-DQA1*05:07-DQB1*06:12', 'HLA-DQA1*05:07-DQB1*06:14', 'HLA-DQA1*05:07-DQB1*06:15', 'HLA-DQA1*05:07-DQB1*06:16',
         'HLA-DQA1*05:07-DQB1*06:17', 'HLA-DQA1*05:07-DQB1*06:18',
         'HLA-DQA1*05:07-DQB1*06:19', 'HLA-DQA1*05:07-DQB1*06:21', 'HLA-DQA1*05:07-DQB1*06:22', 'HLA-DQA1*05:07-DQB1*06:23',
         'HLA-DQA1*05:07-DQB1*06:24', 'HLA-DQA1*05:07-DQB1*06:25',
         'HLA-DQA1*05:07-DQB1*06:27', 'HLA-DQA1*05:07-DQB1*06:28', 'HLA-DQA1*05:07-DQB1*06:29', 'HLA-DQA1*05:07-DQB1*06:30',
         'HLA-DQA1*05:07-DQB1*06:31', 'HLA-DQA1*05:07-DQB1*06:32',
         'HLA-DQA1*05:07-DQB1*06:33', 'HLA-DQA1*05:07-DQB1*06:34', 'HLA-DQA1*05:07-DQB1*06:35', 'HLA-DQA1*05:07-DQB1*06:36',
         'HLA-DQA1*05:07-DQB1*06:37', 'HLA-DQA1*05:07-DQB1*06:38',
         'HLA-DQA1*05:07-DQB1*06:39', 'HLA-DQA1*05:07-DQB1*06:40', 'HLA-DQA1*05:07-DQB1*06:41', 'HLA-DQA1*05:07-DQB1*06:42',
         'HLA-DQA1*05:07-DQB1*06:43', 'HLA-DQA1*05:07-DQB1*06:44',
         'HLA-DQA1*05:08-DQB1*02:01', 'HLA-DQA1*05:08-DQB1*02:02', 'HLA-DQA1*05:08-DQB1*02:03', 'HLA-DQA1*05:08-DQB1*02:04',
         'HLA-DQA1*05:08-DQB1*02:05', 'HLA-DQA1*05:08-DQB1*02:06',
         'HLA-DQA1*05:08-DQB1*03:01', 'HLA-DQA1*05:08-DQB1*03:02', 'HLA-DQA1*05:08-DQB1*03:03', 'HLA-DQA1*05:08-DQB1*03:04',
         'HLA-DQA1*05:08-DQB1*03:05', 'HLA-DQA1*05:08-DQB1*03:06',
         'HLA-DQA1*05:08-DQB1*03:07', 'HLA-DQA1*05:08-DQB1*03:08', 'HLA-DQA1*05:08-DQB1*03:09', 'HLA-DQA1*05:08-DQB1*03:10',
         'HLA-DQA1*05:08-DQB1*03:11', 'HLA-DQA1*05:08-DQB1*03:12',
         'HLA-DQA1*05:08-DQB1*03:13', 'HLA-DQA1*05:08-DQB1*03:14', 'HLA-DQA1*05:08-DQB1*03:15', 'HLA-DQA1*05:08-DQB1*03:16',
         'HLA-DQA1*05:08-DQB1*03:17', 'HLA-DQA1*05:08-DQB1*03:18',
         'HLA-DQA1*05:08-DQB1*03:19', 'HLA-DQA1*05:08-DQB1*03:20', 'HLA-DQA1*05:08-DQB1*03:21', 'HLA-DQA1*05:08-DQB1*03:22',
         'HLA-DQA1*05:08-DQB1*03:23', 'HLA-DQA1*05:08-DQB1*03:24',
         'HLA-DQA1*05:08-DQB1*03:25', 'HLA-DQA1*05:08-DQB1*03:26', 'HLA-DQA1*05:08-DQB1*03:27', 'HLA-DQA1*05:08-DQB1*03:28',
         'HLA-DQA1*05:08-DQB1*03:29', 'HLA-DQA1*05:08-DQB1*03:30',
         'HLA-DQA1*05:08-DQB1*03:31', 'HLA-DQA1*05:08-DQB1*03:32', 'HLA-DQA1*05:08-DQB1*03:33', 'HLA-DQA1*05:08-DQB1*03:34',
         'HLA-DQA1*05:08-DQB1*03:35', 'HLA-DQA1*05:08-DQB1*03:36',
         'HLA-DQA1*05:08-DQB1*03:37', 'HLA-DQA1*05:08-DQB1*03:38', 'HLA-DQA1*05:08-DQB1*04:01', 'HLA-DQA1*05:08-DQB1*04:02',
         'HLA-DQA1*05:08-DQB1*04:03', 'HLA-DQA1*05:08-DQB1*04:04',
         'HLA-DQA1*05:08-DQB1*04:05', 'HLA-DQA1*05:08-DQB1*04:06', 'HLA-DQA1*05:08-DQB1*04:07', 'HLA-DQA1*05:08-DQB1*04:08',
         'HLA-DQA1*05:08-DQB1*05:01', 'HLA-DQA1*05:08-DQB1*05:02',
         'HLA-DQA1*05:08-DQB1*05:03', 'HLA-DQA1*05:08-DQB1*05:05', 'HLA-DQA1*05:08-DQB1*05:06', 'HLA-DQA1*05:08-DQB1*05:07',
         'HLA-DQA1*05:08-DQB1*05:08', 'HLA-DQA1*05:08-DQB1*05:09',
         'HLA-DQA1*05:08-DQB1*05:10', 'HLA-DQA1*05:08-DQB1*05:11', 'HLA-DQA1*05:08-DQB1*05:12', 'HLA-DQA1*05:08-DQB1*05:13',
         'HLA-DQA1*05:08-DQB1*05:14', 'HLA-DQA1*05:08-DQB1*06:01',
         'HLA-DQA1*05:08-DQB1*06:02', 'HLA-DQA1*05:08-DQB1*06:03', 'HLA-DQA1*05:08-DQB1*06:04', 'HLA-DQA1*05:08-DQB1*06:07',
         'HLA-DQA1*05:08-DQB1*06:08', 'HLA-DQA1*05:08-DQB1*06:09',
         'HLA-DQA1*05:08-DQB1*06:10', 'HLA-DQA1*05:08-DQB1*06:11', 'HLA-DQA1*05:08-DQB1*06:12', 'HLA-DQA1*05:08-DQB1*06:14',
         'HLA-DQA1*05:08-DQB1*06:15', 'HLA-DQA1*05:08-DQB1*06:16',
         'HLA-DQA1*05:08-DQB1*06:17', 'HLA-DQA1*05:08-DQB1*06:18', 'HLA-DQA1*05:08-DQB1*06:19', 'HLA-DQA1*05:08-DQB1*06:21',
         'HLA-DQA1*05:08-DQB1*06:22', 'HLA-DQA1*05:08-DQB1*06:23',
         'HLA-DQA1*05:08-DQB1*06:24', 'HLA-DQA1*05:08-DQB1*06:25', 'HLA-DQA1*05:08-DQB1*06:27', 'HLA-DQA1*05:08-DQB1*06:28',
         'HLA-DQA1*05:08-DQB1*06:29', 'HLA-DQA1*05:08-DQB1*06:30',
         'HLA-DQA1*05:08-DQB1*06:31', 'HLA-DQA1*05:08-DQB1*06:32', 'HLA-DQA1*05:08-DQB1*06:33', 'HLA-DQA1*05:08-DQB1*06:34',
         'HLA-DQA1*05:08-DQB1*06:35', 'HLA-DQA1*05:08-DQB1*06:36',
         'HLA-DQA1*05:08-DQB1*06:37', 'HLA-DQA1*05:08-DQB1*06:38', 'HLA-DQA1*05:08-DQB1*06:39', 'HLA-DQA1*05:08-DQB1*06:40',
         'HLA-DQA1*05:08-DQB1*06:41', 'HLA-DQA1*05:08-DQB1*06:42',
         'HLA-DQA1*05:08-DQB1*06:43', 'HLA-DQA1*05:08-DQB1*06:44', 'HLA-DQA1*05:09-DQB1*02:01', 'HLA-DQA1*05:09-DQB1*02:02',
         'HLA-DQA1*05:09-DQB1*02:03', 'HLA-DQA1*05:09-DQB1*02:04',
         'HLA-DQA1*05:09-DQB1*02:05', 'HLA-DQA1*05:09-DQB1*02:06', 'HLA-DQA1*05:09-DQB1*03:01', 'HLA-DQA1*05:09-DQB1*03:02',
         'HLA-DQA1*05:09-DQB1*03:03', 'HLA-DQA1*05:09-DQB1*03:04',
         'HLA-DQA1*05:09-DQB1*03:05', 'HLA-DQA1*05:09-DQB1*03:06', 'HLA-DQA1*05:09-DQB1*03:07', 'HLA-DQA1*05:09-DQB1*03:08',
         'HLA-DQA1*05:09-DQB1*03:09', 'HLA-DQA1*05:09-DQB1*03:10',
         'HLA-DQA1*05:09-DQB1*03:11', 'HLA-DQA1*05:09-DQB1*03:12', 'HLA-DQA1*05:09-DQB1*03:13', 'HLA-DQA1*05:09-DQB1*03:14',
         'HLA-DQA1*05:09-DQB1*03:15', 'HLA-DQA1*05:09-DQB1*03:16',
         'HLA-DQA1*05:09-DQB1*03:17', 'HLA-DQA1*05:09-DQB1*03:18', 'HLA-DQA1*05:09-DQB1*03:19', 'HLA-DQA1*05:09-DQB1*03:20',
         'HLA-DQA1*05:09-DQB1*03:21', 'HLA-DQA1*05:09-DQB1*03:22',
         'HLA-DQA1*05:09-DQB1*03:23', 'HLA-DQA1*05:09-DQB1*03:24', 'HLA-DQA1*05:09-DQB1*03:25', 'HLA-DQA1*05:09-DQB1*03:26',
         'HLA-DQA1*05:09-DQB1*03:27', 'HLA-DQA1*05:09-DQB1*03:28',
         'HLA-DQA1*05:09-DQB1*03:29', 'HLA-DQA1*05:09-DQB1*03:30', 'HLA-DQA1*05:09-DQB1*03:31', 'HLA-DQA1*05:09-DQB1*03:32',
         'HLA-DQA1*05:09-DQB1*03:33', 'HLA-DQA1*05:09-DQB1*03:34',
         'HLA-DQA1*05:09-DQB1*03:35', 'HLA-DQA1*05:09-DQB1*03:36', 'HLA-DQA1*05:09-DQB1*03:37', 'HLA-DQA1*05:09-DQB1*03:38',
         'HLA-DQA1*05:09-DQB1*04:01', 'HLA-DQA1*05:09-DQB1*04:02',
         'HLA-DQA1*05:09-DQB1*04:03', 'HLA-DQA1*05:09-DQB1*04:04', 'HLA-DQA1*05:09-DQB1*04:05', 'HLA-DQA1*05:09-DQB1*04:06',
         'HLA-DQA1*05:09-DQB1*04:07', 'HLA-DQA1*05:09-DQB1*04:08',
         'HLA-DQA1*05:09-DQB1*05:01', 'HLA-DQA1*05:09-DQB1*05:02', 'HLA-DQA1*05:09-DQB1*05:03', 'HLA-DQA1*05:09-DQB1*05:05',
         'HLA-DQA1*05:09-DQB1*05:06', 'HLA-DQA1*05:09-DQB1*05:07',
         'HLA-DQA1*05:09-DQB1*05:08', 'HLA-DQA1*05:09-DQB1*05:09', 'HLA-DQA1*05:09-DQB1*05:10', 'HLA-DQA1*05:09-DQB1*05:11',
         'HLA-DQA1*05:09-DQB1*05:12', 'HLA-DQA1*05:09-DQB1*05:13',
         'HLA-DQA1*05:09-DQB1*05:14', 'HLA-DQA1*05:09-DQB1*06:01', 'HLA-DQA1*05:09-DQB1*06:02', 'HLA-DQA1*05:09-DQB1*06:03',
         'HLA-DQA1*05:09-DQB1*06:04', 'HLA-DQA1*05:09-DQB1*06:07',
         'HLA-DQA1*05:09-DQB1*06:08', 'HLA-DQA1*05:09-DQB1*06:09', 'HLA-DQA1*05:09-DQB1*06:10', 'HLA-DQA1*05:09-DQB1*06:11',
         'HLA-DQA1*05:09-DQB1*06:12', 'HLA-DQA1*05:09-DQB1*06:14',
         'HLA-DQA1*05:09-DQB1*06:15', 'HLA-DQA1*05:09-DQB1*06:16', 'HLA-DQA1*05:09-DQB1*06:17', 'HLA-DQA1*05:09-DQB1*06:18',
         'HLA-DQA1*05:09-DQB1*06:19', 'HLA-DQA1*05:09-DQB1*06:21',
         'HLA-DQA1*05:09-DQB1*06:22', 'HLA-DQA1*05:09-DQB1*06:23', 'HLA-DQA1*05:09-DQB1*06:24', 'HLA-DQA1*05:09-DQB1*06:25',
         'HLA-DQA1*05:09-DQB1*06:27', 'HLA-DQA1*05:09-DQB1*06:28',
         'HLA-DQA1*05:09-DQB1*06:29', 'HLA-DQA1*05:09-DQB1*06:30', 'HLA-DQA1*05:09-DQB1*06:31', 'HLA-DQA1*05:09-DQB1*06:32',
         'HLA-DQA1*05:09-DQB1*06:33', 'HLA-DQA1*05:09-DQB1*06:34',
         'HLA-DQA1*05:09-DQB1*06:35', 'HLA-DQA1*05:09-DQB1*06:36', 'HLA-DQA1*05:09-DQB1*06:37', 'HLA-DQA1*05:09-DQB1*06:38',
         'HLA-DQA1*05:09-DQB1*06:39', 'HLA-DQA1*05:09-DQB1*06:40',
         'HLA-DQA1*05:09-DQB1*06:41', 'HLA-DQA1*05:09-DQB1*06:42', 'HLA-DQA1*05:09-DQB1*06:43', 'HLA-DQA1*05:09-DQB1*06:44',
         'HLA-DQA1*05:10-DQB1*02:01', 'HLA-DQA1*05:10-DQB1*02:02',
         'HLA-DQA1*05:10-DQB1*02:03', 'HLA-DQA1*05:10-DQB1*02:04', 'HLA-DQA1*05:10-DQB1*02:05', 'HLA-DQA1*05:10-DQB1*02:06',
         'HLA-DQA1*05:10-DQB1*03:01', 'HLA-DQA1*05:10-DQB1*03:02',
         'HLA-DQA1*05:10-DQB1*03:03', 'HLA-DQA1*05:10-DQB1*03:04', 'HLA-DQA1*05:10-DQB1*03:05', 'HLA-DQA1*05:10-DQB1*03:06',
         'HLA-DQA1*05:10-DQB1*03:07', 'HLA-DQA1*05:10-DQB1*03:08',
         'HLA-DQA1*05:10-DQB1*03:09', 'HLA-DQA1*05:10-DQB1*03:10', 'HLA-DQA1*05:10-DQB1*03:11', 'HLA-DQA1*05:10-DQB1*03:12',
         'HLA-DQA1*05:10-DQB1*03:13', 'HLA-DQA1*05:10-DQB1*03:14',
         'HLA-DQA1*05:10-DQB1*03:15', 'HLA-DQA1*05:10-DQB1*03:16', 'HLA-DQA1*05:10-DQB1*03:17', 'HLA-DQA1*05:10-DQB1*03:18',
         'HLA-DQA1*05:10-DQB1*03:19', 'HLA-DQA1*05:10-DQB1*03:20',
         'HLA-DQA1*05:10-DQB1*03:21', 'HLA-DQA1*05:10-DQB1*03:22', 'HLA-DQA1*05:10-DQB1*03:23', 'HLA-DQA1*05:10-DQB1*03:24',
         'HLA-DQA1*05:10-DQB1*03:25', 'HLA-DQA1*05:10-DQB1*03:26',
         'HLA-DQA1*05:10-DQB1*03:27', 'HLA-DQA1*05:10-DQB1*03:28', 'HLA-DQA1*05:10-DQB1*03:29', 'HLA-DQA1*05:10-DQB1*03:30',
         'HLA-DQA1*05:10-DQB1*03:31', 'HLA-DQA1*05:10-DQB1*03:32',
         'HLA-DQA1*05:10-DQB1*03:33', 'HLA-DQA1*05:10-DQB1*03:34', 'HLA-DQA1*05:10-DQB1*03:35', 'HLA-DQA1*05:10-DQB1*03:36',
         'HLA-DQA1*05:10-DQB1*03:37', 'HLA-DQA1*05:10-DQB1*03:38',
         'HLA-DQA1*05:10-DQB1*04:01', 'HLA-DQA1*05:10-DQB1*04:02', 'HLA-DQA1*05:10-DQB1*04:03', 'HLA-DQA1*05:10-DQB1*04:04',
         'HLA-DQA1*05:10-DQB1*04:05', 'HLA-DQA1*05:10-DQB1*04:06',
         'HLA-DQA1*05:10-DQB1*04:07', 'HLA-DQA1*05:10-DQB1*04:08', 'HLA-DQA1*05:10-DQB1*05:01', 'HLA-DQA1*05:10-DQB1*05:02',
         'HLA-DQA1*05:10-DQB1*05:03', 'HLA-DQA1*05:10-DQB1*05:05',
         'HLA-DQA1*05:10-DQB1*05:06', 'HLA-DQA1*05:10-DQB1*05:07', 'HLA-DQA1*05:10-DQB1*05:08', 'HLA-DQA1*05:10-DQB1*05:09',
         'HLA-DQA1*05:10-DQB1*05:10', 'HLA-DQA1*05:10-DQB1*05:11',
         'HLA-DQA1*05:10-DQB1*05:12', 'HLA-DQA1*05:10-DQB1*05:13', 'HLA-DQA1*05:10-DQB1*05:14', 'HLA-DQA1*05:10-DQB1*06:01',
         'HLA-DQA1*05:10-DQB1*06:02', 'HLA-DQA1*05:10-DQB1*06:03',
         'HLA-DQA1*05:10-DQB1*06:04', 'HLA-DQA1*05:10-DQB1*06:07', 'HLA-DQA1*05:10-DQB1*06:08', 'HLA-DQA1*05:10-DQB1*06:09',
         'HLA-DQA1*05:10-DQB1*06:10', 'HLA-DQA1*05:10-DQB1*06:11',
         'HLA-DQA1*05:10-DQB1*06:12', 'HLA-DQA1*05:10-DQB1*06:14', 'HLA-DQA1*05:10-DQB1*06:15', 'HLA-DQA1*05:10-DQB1*06:16',
         'HLA-DQA1*05:10-DQB1*06:17', 'HLA-DQA1*05:10-DQB1*06:18',
         'HLA-DQA1*05:10-DQB1*06:19', 'HLA-DQA1*05:10-DQB1*06:21', 'HLA-DQA1*05:10-DQB1*06:22', 'HLA-DQA1*05:10-DQB1*06:23',
         'HLA-DQA1*05:10-DQB1*06:24', 'HLA-DQA1*05:10-DQB1*06:25',
         'HLA-DQA1*05:10-DQB1*06:27', 'HLA-DQA1*05:10-DQB1*06:28', 'HLA-DQA1*05:10-DQB1*06:29', 'HLA-DQA1*05:10-DQB1*06:30',
         'HLA-DQA1*05:10-DQB1*06:31', 'HLA-DQA1*05:10-DQB1*06:32',
         'HLA-DQA1*05:10-DQB1*06:33', 'HLA-DQA1*05:10-DQB1*06:34', 'HLA-DQA1*05:10-DQB1*06:35', 'HLA-DQA1*05:10-DQB1*06:36',
         'HLA-DQA1*05:10-DQB1*06:37', 'HLA-DQA1*05:10-DQB1*06:38',
         'HLA-DQA1*05:10-DQB1*06:39', 'HLA-DQA1*05:10-DQB1*06:40', 'HLA-DQA1*05:10-DQB1*06:41', 'HLA-DQA1*05:10-DQB1*06:42',
         'HLA-DQA1*05:10-DQB1*06:43', 'HLA-DQA1*05:10-DQB1*06:44',
         'HLA-DQA1*05:11-DQB1*02:01', 'HLA-DQA1*05:11-DQB1*02:02', 'HLA-DQA1*05:11-DQB1*02:03', 'HLA-DQA1*05:11-DQB1*02:04',
         'HLA-DQA1*05:11-DQB1*02:05', 'HLA-DQA1*05:11-DQB1*02:06',
         'HLA-DQA1*05:11-DQB1*03:01', 'HLA-DQA1*05:11-DQB1*03:02', 'HLA-DQA1*05:11-DQB1*03:03', 'HLA-DQA1*05:11-DQB1*03:04',
         'HLA-DQA1*05:11-DQB1*03:05', 'HLA-DQA1*05:11-DQB1*03:06',
         'HLA-DQA1*05:11-DQB1*03:07', 'HLA-DQA1*05:11-DQB1*03:08', 'HLA-DQA1*05:11-DQB1*03:09', 'HLA-DQA1*05:11-DQB1*03:10',
         'HLA-DQA1*05:11-DQB1*03:11', 'HLA-DQA1*05:11-DQB1*03:12',
         'HLA-DQA1*05:11-DQB1*03:13', 'HLA-DQA1*05:11-DQB1*03:14', 'HLA-DQA1*05:11-DQB1*03:15', 'HLA-DQA1*05:11-DQB1*03:16',
         'HLA-DQA1*05:11-DQB1*03:17', 'HLA-DQA1*05:11-DQB1*03:18',
         'HLA-DQA1*05:11-DQB1*03:19', 'HLA-DQA1*05:11-DQB1*03:20', 'HLA-DQA1*05:11-DQB1*03:21', 'HLA-DQA1*05:11-DQB1*03:22',
         'HLA-DQA1*05:11-DQB1*03:23', 'HLA-DQA1*05:11-DQB1*03:24',
         'HLA-DQA1*05:11-DQB1*03:25', 'HLA-DQA1*05:11-DQB1*03:26', 'HLA-DQA1*05:11-DQB1*03:27', 'HLA-DQA1*05:11-DQB1*03:28',
         'HLA-DQA1*05:11-DQB1*03:29', 'HLA-DQA1*05:11-DQB1*03:30',
         'HLA-DQA1*05:11-DQB1*03:31', 'HLA-DQA1*05:11-DQB1*03:32', 'HLA-DQA1*05:11-DQB1*03:33', 'HLA-DQA1*05:11-DQB1*03:34',
         'HLA-DQA1*05:11-DQB1*03:35', 'HLA-DQA1*05:11-DQB1*03:36',
         'HLA-DQA1*05:11-DQB1*03:37', 'HLA-DQA1*05:11-DQB1*03:38', 'HLA-DQA1*05:11-DQB1*04:01', 'HLA-DQA1*05:11-DQB1*04:02',
         'HLA-DQA1*05:11-DQB1*04:03', 'HLA-DQA1*05:11-DQB1*04:04',
         'HLA-DQA1*05:11-DQB1*04:05', 'HLA-DQA1*05:11-DQB1*04:06', 'HLA-DQA1*05:11-DQB1*04:07', 'HLA-DQA1*05:11-DQB1*04:08',
         'HLA-DQA1*05:11-DQB1*05:01', 'HLA-DQA1*05:11-DQB1*05:02',
         'HLA-DQA1*05:11-DQB1*05:03', 'HLA-DQA1*05:11-DQB1*05:05', 'HLA-DQA1*05:11-DQB1*05:06', 'HLA-DQA1*05:11-DQB1*05:07',
         'HLA-DQA1*05:11-DQB1*05:08', 'HLA-DQA1*05:11-DQB1*05:09',
         'HLA-DQA1*05:11-DQB1*05:10', 'HLA-DQA1*05:11-DQB1*05:11', 'HLA-DQA1*05:11-DQB1*05:12', 'HLA-DQA1*05:11-DQB1*05:13',
         'HLA-DQA1*05:11-DQB1*05:14', 'HLA-DQA1*05:11-DQB1*06:01',
         'HLA-DQA1*05:11-DQB1*06:02', 'HLA-DQA1*05:11-DQB1*06:03', 'HLA-DQA1*05:11-DQB1*06:04', 'HLA-DQA1*05:11-DQB1*06:07',
         'HLA-DQA1*05:11-DQB1*06:08', 'HLA-DQA1*05:11-DQB1*06:09',
         'HLA-DQA1*05:11-DQB1*06:10', 'HLA-DQA1*05:11-DQB1*06:11', 'HLA-DQA1*05:11-DQB1*06:12', 'HLA-DQA1*05:11-DQB1*06:14',
         'HLA-DQA1*05:11-DQB1*06:15', 'HLA-DQA1*05:11-DQB1*06:16',
         'HLA-DQA1*05:11-DQB1*06:17', 'HLA-DQA1*05:11-DQB1*06:18', 'HLA-DQA1*05:11-DQB1*06:19', 'HLA-DQA1*05:11-DQB1*06:21',
         'HLA-DQA1*05:11-DQB1*06:22', 'HLA-DQA1*05:11-DQB1*06:23',
         'HLA-DQA1*05:11-DQB1*06:24', 'HLA-DQA1*05:11-DQB1*06:25', 'HLA-DQA1*05:11-DQB1*06:27', 'HLA-DQA1*05:11-DQB1*06:28',
         'HLA-DQA1*05:11-DQB1*06:29', 'HLA-DQA1*05:11-DQB1*06:30',
         'HLA-DQA1*05:11-DQB1*06:31', 'HLA-DQA1*05:11-DQB1*06:32', 'HLA-DQA1*05:11-DQB1*06:33', 'HLA-DQA1*05:11-DQB1*06:34',
         'HLA-DQA1*05:11-DQB1*06:35', 'HLA-DQA1*05:11-DQB1*06:36',
         'HLA-DQA1*05:11-DQB1*06:37', 'HLA-DQA1*05:11-DQB1*06:38', 'HLA-DQA1*05:11-DQB1*06:39', 'HLA-DQA1*05:11-DQB1*06:40',
         'HLA-DQA1*05:11-DQB1*06:41', 'HLA-DQA1*05:11-DQB1*06:42',
         'HLA-DQA1*05:11-DQB1*06:43', 'HLA-DQA1*05:11-DQB1*06:44', 'HLA-DQA1*06:01-DQB1*02:01', 'HLA-DQA1*06:01-DQB1*02:02',
         'HLA-DQA1*06:01-DQB1*02:03', 'HLA-DQA1*06:01-DQB1*02:04',
         'HLA-DQA1*06:01-DQB1*02:05', 'HLA-DQA1*06:01-DQB1*02:06', 'HLA-DQA1*06:01-DQB1*03:01', 'HLA-DQA1*06:01-DQB1*03:02',
         'HLA-DQA1*06:01-DQB1*03:03', 'HLA-DQA1*06:01-DQB1*03:04',
         'HLA-DQA1*06:01-DQB1*03:05', 'HLA-DQA1*06:01-DQB1*03:06', 'HLA-DQA1*06:01-DQB1*03:07', 'HLA-DQA1*06:01-DQB1*03:08',
         'HLA-DQA1*06:01-DQB1*03:09', 'HLA-DQA1*06:01-DQB1*03:10',
         'HLA-DQA1*06:01-DQB1*03:11', 'HLA-DQA1*06:01-DQB1*03:12', 'HLA-DQA1*06:01-DQB1*03:13', 'HLA-DQA1*06:01-DQB1*03:14',
         'HLA-DQA1*06:01-DQB1*03:15', 'HLA-DQA1*06:01-DQB1*03:16',
         'HLA-DQA1*06:01-DQB1*03:17', 'HLA-DQA1*06:01-DQB1*03:18', 'HLA-DQA1*06:01-DQB1*03:19', 'HLA-DQA1*06:01-DQB1*03:20',
         'HLA-DQA1*06:01-DQB1*03:21', 'HLA-DQA1*06:01-DQB1*03:22',
         'HLA-DQA1*06:01-DQB1*03:23', 'HLA-DQA1*06:01-DQB1*03:24', 'HLA-DQA1*06:01-DQB1*03:25', 'HLA-DQA1*06:01-DQB1*03:26',
         'HLA-DQA1*06:01-DQB1*03:27', 'HLA-DQA1*06:01-DQB1*03:28',
         'HLA-DQA1*06:01-DQB1*03:29', 'HLA-DQA1*06:01-DQB1*03:30', 'HLA-DQA1*06:01-DQB1*03:31', 'HLA-DQA1*06:01-DQB1*03:32',
         'HLA-DQA1*06:01-DQB1*03:33', 'HLA-DQA1*06:01-DQB1*03:34',
         'HLA-DQA1*06:01-DQB1*03:35', 'HLA-DQA1*06:01-DQB1*03:36', 'HLA-DQA1*06:01-DQB1*03:37', 'HLA-DQA1*06:01-DQB1*03:38',
         'HLA-DQA1*06:01-DQB1*04:01', 'HLA-DQA1*06:01-DQB1*04:02',
         'HLA-DQA1*06:01-DQB1*04:03', 'HLA-DQA1*06:01-DQB1*04:04', 'HLA-DQA1*06:01-DQB1*04:05', 'HLA-DQA1*06:01-DQB1*04:06',
         'HLA-DQA1*06:01-DQB1*04:07', 'HLA-DQA1*06:01-DQB1*04:08',
         'HLA-DQA1*06:01-DQB1*05:01', 'HLA-DQA1*06:01-DQB1*05:02', 'HLA-DQA1*06:01-DQB1*05:03', 'HLA-DQA1*06:01-DQB1*05:05',
         'HLA-DQA1*06:01-DQB1*05:06', 'HLA-DQA1*06:01-DQB1*05:07',
         'HLA-DQA1*06:01-DQB1*05:08', 'HLA-DQA1*06:01-DQB1*05:09', 'HLA-DQA1*06:01-DQB1*05:10', 'HLA-DQA1*06:01-DQB1*05:11',
         'HLA-DQA1*06:01-DQB1*05:12', 'HLA-DQA1*06:01-DQB1*05:13',
         'HLA-DQA1*06:01-DQB1*05:14', 'HLA-DQA1*06:01-DQB1*06:01', 'HLA-DQA1*06:01-DQB1*06:02', 'HLA-DQA1*06:01-DQB1*06:03',
         'HLA-DQA1*06:01-DQB1*06:04', 'HLA-DQA1*06:01-DQB1*06:07',
         'HLA-DQA1*06:01-DQB1*06:08', 'HLA-DQA1*06:01-DQB1*06:09', 'HLA-DQA1*06:01-DQB1*06:10', 'HLA-DQA1*06:01-DQB1*06:11',
         'HLA-DQA1*06:01-DQB1*06:12', 'HLA-DQA1*06:01-DQB1*06:14',
         'HLA-DQA1*06:01-DQB1*06:15', 'HLA-DQA1*06:01-DQB1*06:16', 'HLA-DQA1*06:01-DQB1*06:17', 'HLA-DQA1*06:01-DQB1*06:18',
         'HLA-DQA1*06:01-DQB1*06:19', 'HLA-DQA1*06:01-DQB1*06:21',
         'HLA-DQA1*06:01-DQB1*06:22', 'HLA-DQA1*06:01-DQB1*06:23', 'HLA-DQA1*06:01-DQB1*06:24', 'HLA-DQA1*06:01-DQB1*06:25',
         'HLA-DQA1*06:01-DQB1*06:27', 'HLA-DQA1*06:01-DQB1*06:28',
         'HLA-DQA1*06:01-DQB1*06:29', 'HLA-DQA1*06:01-DQB1*06:30', 'HLA-DQA1*06:01-DQB1*06:31', 'HLA-DQA1*06:01-DQB1*06:32',
         'HLA-DQA1*06:01-DQB1*06:33', 'HLA-DQA1*06:01-DQB1*06:34',
         'HLA-DQA1*06:01-DQB1*06:35', 'HLA-DQA1*06:01-DQB1*06:36', 'HLA-DQA1*06:01-DQB1*06:37', 'HLA-DQA1*06:01-DQB1*06:38',
         'HLA-DQA1*06:01-DQB1*06:39', 'HLA-DQA1*06:01-DQB1*06:40',
         'HLA-DQA1*06:01-DQB1*06:41', 'HLA-DQA1*06:01-DQB1*06:42', 'HLA-DQA1*06:01-DQB1*06:43', 'HLA-DQA1*06:01-DQB1*06:44',
         'HLA-DQA1*06:02-DQB1*02:01', 'HLA-DQA1*06:02-DQB1*02:02',
         'HLA-DQA1*06:02-DQB1*02:03', 'HLA-DQA1*06:02-DQB1*02:04', 'HLA-DQA1*06:02-DQB1*02:05', 'HLA-DQA1*06:02-DQB1*02:06',
         'HLA-DQA1*06:02-DQB1*03:01', 'HLA-DQA1*06:02-DQB1*03:02',
         'HLA-DQA1*06:02-DQB1*03:03', 'HLA-DQA1*06:02-DQB1*03:04', 'HLA-DQA1*06:02-DQB1*03:05', 'HLA-DQA1*06:02-DQB1*03:06',
         'HLA-DQA1*06:02-DQB1*03:07', 'HLA-DQA1*06:02-DQB1*03:08',
         'HLA-DQA1*06:02-DQB1*03:09', 'HLA-DQA1*06:02-DQB1*03:10', 'HLA-DQA1*06:02-DQB1*03:11', 'HLA-DQA1*06:02-DQB1*03:12',
         'HLA-DQA1*06:02-DQB1*03:13', 'HLA-DQA1*06:02-DQB1*03:14',
         'HLA-DQA1*06:02-DQB1*03:15', 'HLA-DQA1*06:02-DQB1*03:16', 'HLA-DQA1*06:02-DQB1*03:17', 'HLA-DQA1*06:02-DQB1*03:18',
         'HLA-DQA1*06:02-DQB1*03:19', 'HLA-DQA1*06:02-DQB1*03:20',
         'HLA-DQA1*06:02-DQB1*03:21', 'HLA-DQA1*06:02-DQB1*03:22', 'HLA-DQA1*06:02-DQB1*03:23', 'HLA-DQA1*06:02-DQB1*03:24',
         'HLA-DQA1*06:02-DQB1*03:25', 'HLA-DQA1*06:02-DQB1*03:26',
         'HLA-DQA1*06:02-DQB1*03:27', 'HLA-DQA1*06:02-DQB1*03:28', 'HLA-DQA1*06:02-DQB1*03:29', 'HLA-DQA1*06:02-DQB1*03:30',
         'HLA-DQA1*06:02-DQB1*03:31', 'HLA-DQA1*06:02-DQB1*03:32',
         'HLA-DQA1*06:02-DQB1*03:33', 'HLA-DQA1*06:02-DQB1*03:34', 'HLA-DQA1*06:02-DQB1*03:35', 'HLA-DQA1*06:02-DQB1*03:36',
         'HLA-DQA1*06:02-DQB1*03:37', 'HLA-DQA1*06:02-DQB1*03:38',
         'HLA-DQA1*06:02-DQB1*04:01', 'HLA-DQA1*06:02-DQB1*04:02', 'HLA-DQA1*06:02-DQB1*04:03', 'HLA-DQA1*06:02-DQB1*04:04',
         'HLA-DQA1*06:02-DQB1*04:05', 'HLA-DQA1*06:02-DQB1*04:06',
         'HLA-DQA1*06:02-DQB1*04:07', 'HLA-DQA1*06:02-DQB1*04:08', 'HLA-DQA1*06:02-DQB1*05:01', 'HLA-DQA1*06:02-DQB1*05:02',
         'HLA-DQA1*06:02-DQB1*05:03', 'HLA-DQA1*06:02-DQB1*05:05',
         'HLA-DQA1*06:02-DQB1*05:06', 'HLA-DQA1*06:02-DQB1*05:07', 'HLA-DQA1*06:02-DQB1*05:08', 'HLA-DQA1*06:02-DQB1*05:09',
         'HLA-DQA1*06:02-DQB1*05:10', 'HLA-DQA1*06:02-DQB1*05:11',
         'HLA-DQA1*06:02-DQB1*05:12', 'HLA-DQA1*06:02-DQB1*05:13', 'HLA-DQA1*06:02-DQB1*05:14', 'HLA-DQA1*06:02-DQB1*06:01',
         'HLA-DQA1*06:02-DQB1*06:02', 'HLA-DQA1*06:02-DQB1*06:03',
         'HLA-DQA1*06:02-DQB1*06:04', 'HLA-DQA1*06:02-DQB1*06:07', 'HLA-DQA1*06:02-DQB1*06:08', 'HLA-DQA1*06:02-DQB1*06:09',
         'HLA-DQA1*06:02-DQB1*06:10', 'HLA-DQA1*06:02-DQB1*06:11',
         'HLA-DQA1*06:02-DQB1*06:12', 'HLA-DQA1*06:02-DQB1*06:14', 'HLA-DQA1*06:02-DQB1*06:15', 'HLA-DQA1*06:02-DQB1*06:16',
         'HLA-DQA1*06:02-DQB1*06:17', 'HLA-DQA1*06:02-DQB1*06:18',
         'HLA-DQA1*06:02-DQB1*06:19', 'HLA-DQA1*06:02-DQB1*06:21', 'HLA-DQA1*06:02-DQB1*06:22', 'HLA-DQA1*06:02-DQB1*06:23',
         'HLA-DQA1*06:02-DQB1*06:24', 'HLA-DQA1*06:02-DQB1*06:25',
         'HLA-DQA1*06:02-DQB1*06:27', 'HLA-DQA1*06:02-DQB1*06:28', 'HLA-DQA1*06:02-DQB1*06:29', 'HLA-DQA1*06:02-DQB1*06:30',
         'HLA-DQA1*06:02-DQB1*06:31', 'HLA-DQA1*06:02-DQB1*06:32',
         'HLA-DQA1*06:02-DQB1*06:33', 'HLA-DQA1*06:02-DQB1*06:34', 'HLA-DQA1*06:02-DQB1*06:35', 'HLA-DQA1*06:02-DQB1*06:36',
         'HLA-DQA1*06:02-DQB1*06:37', 'HLA-DQA1*06:02-DQB1*06:38',
         'HLA-DQA1*06:02-DQB1*06:39', 'HLA-DQA1*06:02-DQB1*06:40', 'HLA-DQA1*06:02-DQB1*06:41', 'HLA-DQA1*06:02-DQB1*06:42',
         'HLA-DQA1*06:02-DQB1*06:43', 'HLA-DQA1*06:02-DQB1*06:44',
         'H-2-Iad', 'H-2-Iab'])

    __version = "3.1"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in set([x for x in next(f) if x != ""])]
 
        next(f)
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCIIPAN_3_1]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCIIPAN_3_1 + i * Offset.NETMHCIIPAN_3_1])
                ranks[a][pep_seq] = float(row[RankIndex.NETMHCIIPAN_3_1 + i * Offset.NETMHCIIPAN_3_1])
                # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}

        return result

class NetMHCIIpan_4_0(NetMHCIIpan_3_1):
    """
    Implementation of NetMHCIIpan 4.0 adapter.

    .. note::

        Reynisson B, Barra C, Kaabinejadian S, Hildebrand WH, Peters B, Nielsen M (2020). Improved prediction of MHC II antigen presentation
        through integration and motif deconvolution of mass spectrometry MHC eluted ligand data.
        Immunogenetics, 1-10.
    """

    __supported_length = frozenset([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    __name = "netmhcIIpan"
    __command = "netMHCIIpan -f {peptides} -inptype 1 -a {alleles} {options} -xls -xlsfile {out}"
    __alleles = frozenset(['HLA-DRB1*01:01', 'HLA-DRB1*01:02', 'HLA-DRB1*01:03', 'HLA-DRB1*01:04', 'HLA-DRB1*01:05',
'HLA-DRB1*01:06', 'HLA-DRB1*01:07', 'HLA-DRB1*01:08', 'HLA-DRB1*01:09', 'HLA-DRB1*01:10',
'HLA-DRB1*01:11', 'HLA-DRB1*01:12', 'HLA-DRB1*01:13', 'HLA-DRB1*01:14', 'HLA-DRB1*01:15',
'HLA-DRB1*01:16', 'HLA-DRB1*01:17', 'HLA-DRB1*01:18', 'HLA-DRB1*01:19', 'HLA-DRB1*01:20',
'HLA-DRB1*01:21', 'HLA-DRB1*01:22', 'HLA-DRB1*01:23', 'HLA-DRB1*01:24', 'HLA-DRB1*01:25',
'HLA-DRB1*01:26', 'HLA-DRB1*01:27', 'HLA-DRB1*01:28', 'HLA-DRB1*01:29', 'HLA-DRB1*01:30',
'HLA-DRB1*01:31', 'HLA-DRB1*01:32', 'HLA-DRB1*03:01', 'HLA-DRB1*03:02', 'HLA-DRB1*03:03',
'HLA-DRB1*03:04', 'HLA-DRB1*03:05', 'HLA-DRB1*03:06', 'HLA-DRB1*03:07', 'HLA-DRB1*03:08',
'HLA-DRB1*03:10', 'HLA-DRB1*03:11', 'HLA-DRB1*03:13', 'HLA-DRB1*03:14', 'HLA-DRB1*03:15',
'HLA-DRB1*03:17', 'HLA-DRB1*03:18', 'HLA-DRB1*03:19', 'HLA-DRB1*03:20', 'HLA-DRB1*03:21',
'HLA-DRB1*03:22', 'HLA-DRB1*03:23', 'HLA-DRB1*03:24', 'HLA-DRB1*03:25', 'HLA-DRB1*03:26',
'HLA-DRB1*03:27', 'HLA-DRB1*03:28', 'HLA-DRB1*03:29', 'HLA-DRB1*03:30', 'HLA-DRB1*03:31',
'HLA-DRB1*03:32', 'HLA-DRB1*03:33', 'HLA-DRB1*03:34', 'HLA-DRB1*03:35', 'HLA-DRB1*03:36',
'HLA-DRB1*03:37', 'HLA-DRB1*03:38', 'HLA-DRB1*03:39', 'HLA-DRB1*03:40', 'HLA-DRB1*03:41',
'HLA-DRB1*03:42', 'HLA-DRB1*03:43', 'HLA-DRB1*03:44', 'HLA-DRB1*03:45', 'HLA-DRB1*03:46',
'HLA-DRB1*03:47', 'HLA-DRB1*03:48', 'HLA-DRB1*03:49', 'HLA-DRB1*03:50', 'HLA-DRB1*03:51',
'HLA-DRB1*03:52', 'HLA-DRB1*03:53', 'HLA-DRB1*03:54', 'HLA-DRB1*03:55', 'HLA-DRB1*04:01',
'HLA-DRB1*04:02', 'HLA-DRB1*04:03', 'HLA-DRB1*04:04', 'HLA-DRB1*04:05', 'HLA-DRB1*04:06',
'HLA-DRB1*04:07', 'HLA-DRB1*04:08', 'HLA-DRB1*04:09', 'HLA-DRB1*04:10', 'HLA-DRB1*04:11',
'HLA-DRB1*04:12', 'HLA-DRB1*04:13', 'HLA-DRB1*04:14', 'HLA-DRB1*04:15', 'HLA-DRB1*04:16',
'HLA-DRB1*04:17', 'HLA-DRB1*04:18', 'HLA-DRB1*04:19', 'HLA-DRB1*04:21', 'HLA-DRB1*04:22',
'HLA-DRB1*04:23', 'HLA-DRB1*04:24', 'HLA-DRB1*04:26', 'HLA-DRB1*04:27', 'HLA-DRB1*04:28',
'HLA-DRB1*04:29', 'HLA-DRB1*04:30', 'HLA-DRB1*04:31', 'HLA-DRB1*04:33', 'HLA-DRB1*04:34',
'HLA-DRB1*04:35', 'HLA-DRB1*04:36', 'HLA-DRB1*04:37', 'HLA-DRB1*04:38', 'HLA-DRB1*04:39',
'HLA-DRB1*04:40', 'HLA-DRB1*04:41', 'HLA-DRB1*04:42', 'HLA-DRB1*04:43', 'HLA-DRB1*04:44',
'HLA-DRB1*04:45', 'HLA-DRB1*04:46', 'HLA-DRB1*04:47', 'HLA-DRB1*04:48', 'HLA-DRB1*04:49',
'HLA-DRB1*04:50', 'HLA-DRB1*04:51', 'HLA-DRB1*04:52', 'HLA-DRB1*04:53', 'HLA-DRB1*04:54',
'HLA-DRB1*04:55', 'HLA-DRB1*04:56', 'HLA-DRB1*04:57', 'HLA-DRB1*04:58', 'HLA-DRB1*04:59',
'HLA-DRB1*04:60', 'HLA-DRB1*04:61', 'HLA-DRB1*04:62', 'HLA-DRB1*04:63', 'HLA-DRB1*04:64',
'HLA-DRB1*04:65', 'HLA-DRB1*04:66', 'HLA-DRB1*04:67', 'HLA-DRB1*04:68', 'HLA-DRB1*04:69',
'HLA-DRB1*04:70', 'HLA-DRB1*04:71', 'HLA-DRB1*04:72', 'HLA-DRB1*04:73', 'HLA-DRB1*04:74',
'HLA-DRB1*04:75', 'HLA-DRB1*04:76', 'HLA-DRB1*04:77', 'HLA-DRB1*04:78', 'HLA-DRB1*04:79',
'HLA-DRB1*04:80', 'HLA-DRB1*04:82', 'HLA-DRB1*04:83', 'HLA-DRB1*04:84', 'HLA-DRB1*04:85',
'HLA-DRB1*04:86', 'HLA-DRB1*04:87', 'HLA-DRB1*04:88', 'HLA-DRB1*04:89', 'HLA-DRB1*04:91',
'HLA-DRB1*07:01', 'HLA-DRB1*07:03', 'HLA-DRB1*07:04', 'HLA-DRB1*07:05', 'HLA-DRB1*07:06',
'HLA-DRB1*07:07', 'HLA-DRB1*07:08', 'HLA-DRB1*07:09', 'HLA-DRB1*07:11', 'HLA-DRB1*07:12',
'HLA-DRB1*07:13', 'HLA-DRB1*07:14', 'HLA-DRB1*07:15', 'HLA-DRB1*07:16', 'HLA-DRB1*07:17',
'HLA-DRB1*07:19', 'HLA-DRB1*08:01', 'HLA-DRB1*08:02', 'HLA-DRB1*08:03', 'HLA-DRB1*08:04',
'HLA-DRB1*08:05', 'HLA-DRB1*08:06', 'HLA-DRB1*08:07', 'HLA-DRB1*08:08', 'HLA-DRB1*08:09',
'HLA-DRB1*08:10', 'HLA-DRB1*08:11', 'HLA-DRB1*08:12', 'HLA-DRB1*08:13', 'HLA-DRB1*08:14',
'HLA-DRB1*08:15', 'HLA-DRB1*08:16', 'HLA-DRB1*08:18', 'HLA-DRB1*08:19', 'HLA-DRB1*08:20',
'HLA-DRB1*08:21', 'HLA-DRB1*08:22', 'HLA-DRB1*08:23', 'HLA-DRB1*08:24', 'HLA-DRB1*08:25',
'HLA-DRB1*08:26', 'HLA-DRB1*08:27', 'HLA-DRB1*08:28', 'HLA-DRB1*08:29', 'HLA-DRB1*08:30',
'HLA-DRB1*08:31', 'HLA-DRB1*08:32', 'HLA-DRB1*08:33', 'HLA-DRB1*08:34', 'HLA-DRB1*08:35',
'HLA-DRB1*08:36', 'HLA-DRB1*08:37', 'HLA-DRB1*08:38', 'HLA-DRB1*08:39', 'HLA-DRB1*08:40',
'HLA-DRB1*09:01', 'HLA-DRB1*09:02', 'HLA-DRB1*09:03', 'HLA-DRB1*09:04', 'HLA-DRB1*09:05',
'HLA-DRB1*09:06', 'HLA-DRB1*09:07', 'HLA-DRB1*09:08', 'HLA-DRB1*09:09', 'HLA-DRB1*10:01',
'HLA-DRB1*10:02', 'HLA-DRB1*10:03', 'HLA-DRB1*11:01', 'HLA-DRB1*11:02', 'HLA-DRB1*11:03',
'HLA-DRB1*11:04', 'HLA-DRB1*11:05', 'HLA-DRB1*11:06', 'HLA-DRB1*11:07', 'HLA-DRB1*11:08',
'HLA-DRB1*11:09', 'HLA-DRB1*11:10', 'HLA-DRB1*11:11', 'HLA-DRB1*11:12', 'HLA-DRB1*11:13',
'HLA-DRB1*11:14', 'HLA-DRB1*11:15', 'HLA-DRB1*11:16', 'HLA-DRB1*11:17', 'HLA-DRB1*11:18',
'HLA-DRB1*11:19', 'HLA-DRB1*11:20', 'HLA-DRB1*11:21', 'HLA-DRB1*11:24', 'HLA-DRB1*11:25',
'HLA-DRB1*11:27', 'HLA-DRB1*11:28', 'HLA-DRB1*11:29', 'HLA-DRB1*11:30', 'HLA-DRB1*11:31',
'HLA-DRB1*11:32', 'HLA-DRB1*11:33', 'HLA-DRB1*11:34', 'HLA-DRB1*11:35', 'HLA-DRB1*11:36',
'HLA-DRB1*11:37', 'HLA-DRB1*11:38', 'HLA-DRB1*11:39', 'HLA-DRB1*11:41', 'HLA-DRB1*11:42',
'HLA-DRB1*11:43', 'HLA-DRB1*11:44', 'HLA-DRB1*11:45', 'HLA-DRB1*11:46', 'HLA-DRB1*11:47',
'HLA-DRB1*11:48', 'HLA-DRB1*11:49', 'HLA-DRB1*11:50', 'HLA-DRB1*11:51', 'HLA-DRB1*11:52',
'HLA-DRB1*11:53', 'HLA-DRB1*11:54', 'HLA-DRB1*11:55', 'HLA-DRB1*11:56', 'HLA-DRB1*11:57',
'HLA-DRB1*11:58', 'HLA-DRB1*11:59', 'HLA-DRB1*11:60', 'HLA-DRB1*11:61', 'HLA-DRB1*11:62',
'HLA-DRB1*11:63', 'HLA-DRB1*11:64', 'HLA-DRB1*11:65', 'HLA-DRB1*11:66', 'HLA-DRB1*11:67',
'HLA-DRB1*11:68', 'HLA-DRB1*11:69', 'HLA-DRB1*11:70', 'HLA-DRB1*11:72', 'HLA-DRB1*11:73',
'HLA-DRB1*11:74', 'HLA-DRB1*11:75', 'HLA-DRB1*11:76', 'HLA-DRB1*11:77', 'HLA-DRB1*11:78',
'HLA-DRB1*11:79', 'HLA-DRB1*11:80', 'HLA-DRB1*11:81', 'HLA-DRB1*11:82', 'HLA-DRB1*11:83',
'HLA-DRB1*11:84', 'HLA-DRB1*11:85', 'HLA-DRB1*11:86', 'HLA-DRB1*11:87', 'HLA-DRB1*11:88',
'HLA-DRB1*11:89', 'HLA-DRB1*11:90', 'HLA-DRB1*11:91', 'HLA-DRB1*11:92', 'HLA-DRB1*11:93',
'HLA-DRB1*11:94', 'HLA-DRB1*11:95', 'HLA-DRB1*11:96', 'HLA-DRB1*12:01', 'HLA-DRB1*12:02',
'HLA-DRB1*12:03', 'HLA-DRB1*12:04', 'HLA-DRB1*12:05', 'HLA-DRB1*12:06', 'HLA-DRB1*12:07',
'HLA-DRB1*12:08', 'HLA-DRB1*12:09', 'HLA-DRB1*12:10', 'HLA-DRB1*12:11', 'HLA-DRB1*12:12',
'HLA-DRB1*12:13', 'HLA-DRB1*12:14', 'HLA-DRB1*12:15', 'HLA-DRB1*12:16', 'HLA-DRB1*12:17',
'HLA-DRB1*12:18', 'HLA-DRB1*12:19', 'HLA-DRB1*12:20', 'HLA-DRB1*12:21', 'HLA-DRB1*12:22',
'HLA-DRB1*12:23', 'HLA-DRB1*13:01', 'HLA-DRB1*13:02', 'HLA-DRB1*13:03', 'HLA-DRB1*13:04',
'HLA-DRB1*13:05', 'HLA-DRB1*13:06', 'HLA-DRB1*13:07', 'HLA-DRB1*13:08', 'HLA-DRB1*13:09',
'HLA-DRB1*13:10', 'HLA-DRB1*13:100', 'HLA-DRB1*13:101', 'HLA-DRB1*13:11', 'HLA-DRB1*13:12',
'HLA-DRB1*13:13', 'HLA-DRB1*13:14', 'HLA-DRB1*13:15', 'HLA-DRB1*13:16', 'HLA-DRB1*13:17',
'HLA-DRB1*13:18', 'HLA-DRB1*13:19', 'HLA-DRB1*13:20', 'HLA-DRB1*13:21', 'HLA-DRB1*13:22',
'HLA-DRB1*13:23', 'HLA-DRB1*13:24', 'HLA-DRB1*13:26', 'HLA-DRB1*13:27', 'HLA-DRB1*13:29',
'HLA-DRB1*13:30', 'HLA-DRB1*13:31', 'HLA-DRB1*13:32', 'HLA-DRB1*13:33', 'HLA-DRB1*13:34',
'HLA-DRB1*13:35', 'HLA-DRB1*13:36', 'HLA-DRB1*13:37', 'HLA-DRB1*13:38', 'HLA-DRB1*13:39',
'HLA-DRB1*13:41', 'HLA-DRB1*13:42', 'HLA-DRB1*13:43', 'HLA-DRB1*13:44', 'HLA-DRB1*13:46',
'HLA-DRB1*13:47', 'HLA-DRB1*13:48', 'HLA-DRB1*13:49', 'HLA-DRB1*13:50', 'HLA-DRB1*13:51',
'HLA-DRB1*13:52', 'HLA-DRB1*13:53', 'HLA-DRB1*13:54', 'HLA-DRB1*13:55', 'HLA-DRB1*13:56',
'HLA-DRB1*13:57', 'HLA-DRB1*13:58', 'HLA-DRB1*13:59', 'HLA-DRB1*13:60', 'HLA-DRB1*13:61',
'HLA-DRB1*13:62', 'HLA-DRB1*13:63', 'HLA-DRB1*13:64', 'HLA-DRB1*13:65', 'HLA-DRB1*13:66',
'HLA-DRB1*13:67', 'HLA-DRB1*13:68', 'HLA-DRB1*13:69', 'HLA-DRB1*13:70', 'HLA-DRB1*13:71',
'HLA-DRB1*13:72', 'HLA-DRB1*13:73', 'HLA-DRB1*13:74', 'HLA-DRB1*13:75', 'HLA-DRB1*13:76',
'HLA-DRB1*13:77', 'HLA-DRB1*13:78', 'HLA-DRB1*13:79', 'HLA-DRB1*13:80', 'HLA-DRB1*13:81',
'HLA-DRB1*13:82', 'HLA-DRB1*13:83', 'HLA-DRB1*13:84', 'HLA-DRB1*13:85', 'HLA-DRB1*13:86',
'HLA-DRB1*13:87', 'HLA-DRB1*13:88', 'HLA-DRB1*13:89', 'HLA-DRB1*13:90', 'HLA-DRB1*13:91',
'HLA-DRB1*13:92', 'HLA-DRB1*13:93', 'HLA-DRB1*13:94', 'HLA-DRB1*13:95', 'HLA-DRB1*13:96',
'HLA-DRB1*13:97', 'HLA-DRB1*13:98', 'HLA-DRB1*13:99', 'HLA-DRB1*14:01', 'HLA-DRB1*14:02',
'HLA-DRB1*14:03', 'HLA-DRB1*14:04', 'HLA-DRB1*14:05', 'HLA-DRB1*14:06', 'HLA-DRB1*14:07',
'HLA-DRB1*14:08', 'HLA-DRB1*14:09', 'HLA-DRB1*14:10', 'HLA-DRB1*14:11', 'HLA-DRB1*14:12',
'HLA-DRB1*14:13', 'HLA-DRB1*14:14', 'HLA-DRB1*14:15', 'HLA-DRB1*14:16', 'HLA-DRB1*14:17',
'HLA-DRB1*14:18', 'HLA-DRB1*14:19', 'HLA-DRB1*14:20', 'HLA-DRB1*14:21', 'HLA-DRB1*14:22',
'HLA-DRB1*14:23', 'HLA-DRB1*14:24', 'HLA-DRB1*14:25', 'HLA-DRB1*14:26', 'HLA-DRB1*14:27',
'HLA-DRB1*14:28', 'HLA-DRB1*14:29', 'HLA-DRB1*14:30', 'HLA-DRB1*14:31', 'HLA-DRB1*14:32',
'HLA-DRB1*14:33', 'HLA-DRB1*14:34', 'HLA-DRB1*14:35', 'HLA-DRB1*14:36', 'HLA-DRB1*14:37',
'HLA-DRB1*14:38', 'HLA-DRB1*14:39', 'HLA-DRB1*14:40', 'HLA-DRB1*14:41', 'HLA-DRB1*14:42',
'HLA-DRB1*14:43', 'HLA-DRB1*14:44', 'HLA-DRB1*14:45', 'HLA-DRB1*14:46', 'HLA-DRB1*14:47',
'HLA-DRB1*14:48', 'HLA-DRB1*14:49', 'HLA-DRB1*14:50', 'HLA-DRB1*14:51', 'HLA-DRB1*14:52',
'HLA-DRB1*14:53', 'HLA-DRB1*14:54', 'HLA-DRB1*14:55', 'HLA-DRB1*14:56', 'HLA-DRB1*14:57',
'HLA-DRB1*14:58', 'HLA-DRB1*14:59', 'HLA-DRB1*14:60', 'HLA-DRB1*14:61', 'HLA-DRB1*14:62',
'HLA-DRB1*14:63', 'HLA-DRB1*14:64', 'HLA-DRB1*14:65', 'HLA-DRB1*14:67', 'HLA-DRB1*14:68',
'HLA-DRB1*14:69', 'HLA-DRB1*14:70', 'HLA-DRB1*14:71', 'HLA-DRB1*14:72', 'HLA-DRB1*14:73',
'HLA-DRB1*14:74', 'HLA-DRB1*14:75', 'HLA-DRB1*14:76', 'HLA-DRB1*14:77', 'HLA-DRB1*14:78',
'HLA-DRB1*14:79', 'HLA-DRB1*14:80', 'HLA-DRB1*14:81', 'HLA-DRB1*14:82', 'HLA-DRB1*14:83',
'HLA-DRB1*14:84', 'HLA-DRB1*14:85', 'HLA-DRB1*14:86', 'HLA-DRB1*14:87', 'HLA-DRB1*14:88',
'HLA-DRB1*14:89', 'HLA-DRB1*14:90', 'HLA-DRB1*14:91', 'HLA-DRB1*14:93', 'HLA-DRB1*14:94',
'HLA-DRB1*14:95', 'HLA-DRB1*14:96', 'HLA-DRB1*14:97', 'HLA-DRB1*14:98', 'HLA-DRB1*14:99',
'HLA-DRB1*15:01', 'HLA-DRB1*15:02', 'HLA-DRB1*15:03', 'HLA-DRB1*15:04', 'HLA-DRB1*15:05',
'HLA-DRB1*15:06', 'HLA-DRB1*15:07', 'HLA-DRB1*15:08', 'HLA-DRB1*15:09', 'HLA-DRB1*15:10',
'HLA-DRB1*15:11', 'HLA-DRB1*15:12', 'HLA-DRB1*15:13', 'HLA-DRB1*15:14', 'HLA-DRB1*15:15',
'HLA-DRB1*15:16', 'HLA-DRB1*15:18', 'HLA-DRB1*15:19', 'HLA-DRB1*15:20', 'HLA-DRB1*15:21',
'HLA-DRB1*15:22', 'HLA-DRB1*15:23', 'HLA-DRB1*15:24', 'HLA-DRB1*15:25', 'HLA-DRB1*15:26',
'HLA-DRB1*15:27', 'HLA-DRB1*15:28', 'HLA-DRB1*15:29', 'HLA-DRB1*15:30', 'HLA-DRB1*15:31',
'HLA-DRB1*15:32', 'HLA-DRB1*15:33', 'HLA-DRB1*15:34', 'HLA-DRB1*15:35', 'HLA-DRB1*15:36',
'HLA-DRB1*15:37', 'HLA-DRB1*15:38', 'HLA-DRB1*15:39', 'HLA-DRB1*15:40', 'HLA-DRB1*15:41',
'HLA-DRB1*15:42', 'HLA-DRB1*15:43', 'HLA-DRB1*15:44', 'HLA-DRB1*15:45', 'HLA-DRB1*15:46',
'HLA-DRB1*15:47', 'HLA-DRB1*15:48', 'HLA-DRB1*15:49', 'HLA-DRB1*16:01', 'HLA-DRB1*16:02',
'HLA-DRB1*16:03', 'HLA-DRB1*16:04', 'HLA-DRB1*16:05', 'HLA-DRB1*16:07', 'HLA-DRB1*16:08',
'HLA-DRB1*16:09', 'HLA-DRB1*16:10', 'HLA-DRB1*16:11', 'HLA-DRB1*16:12', 'HLA-DRB1*16:14',
'HLA-DRB1*16:15', 'HLA-DRB1*16:16', 'HLA-DRB3*01:01', 'HLA-DRB3*01:04', 'HLA-DRB3*01:05',
'HLA-DRB3*01:08', 'HLA-DRB3*01:09', 'HLA-DRB3*01:11', 'HLA-DRB3*01:12', 'HLA-DRB3*01:13',
'HLA-DRB3*01:14', 'HLA-DRB3*02:01', 'HLA-DRB3*02:02', 'HLA-DRB3*02:04', 'HLA-DRB3*02:05',
'HLA-DRB3*02:09', 'HLA-DRB3*02:10', 'HLA-DRB3*02:11', 'HLA-DRB3*02:12', 'HLA-DRB3*02:13',
'HLA-DRB3*02:14', 'HLA-DRB3*02:15', 'HLA-DRB3*02:16', 'HLA-DRB3*02:17', 'HLA-DRB3*02:18',
'HLA-DRB3*02:19', 'HLA-DRB3*02:20', 'HLA-DRB3*02:21', 'HLA-DRB3*02:22', 'HLA-DRB3*02:23',
'HLA-DRB3*02:24', 'HLA-DRB3*02:25', 'HLA-DRB3*03:01', 'HLA-DRB3*03:03', 'HLA-DRB4*01:01',
'HLA-DRB4*01:03', 'HLA-DRB4*01:04', 'HLA-DRB4*01:06', 'HLA-DRB4*01:07', 'HLA-DRB4*01:08',
'HLA-DRB5*01:01', 'HLA-DRB5*01:02', 'HLA-DRB5*01:03', 'HLA-DRB5*01:04', 'HLA-DRB5*01:05',
'HLA-DRB5*01:06', 'HLA-DRB5*01:08N', 'HLA-DRB5*01:11', 'HLA-DRB5*01:12', 'HLA-DRB5*01:13',
'HLA-DRB5*01:14', 'HLA-DRB5*02:02', 'HLA-DRB5*02:03', 'HLA-DRB5*02:04', 'HLA-DRB5*02:05',
'HLA-DPA1*01:03-DPB1*01:01', 'HLA-DPA1*01:03-DPB1*02:01', 'HLA-DPA1*01:03-DPB1*02:02', 'HLA-DPA1*01:03-DPB1*03:01', 'HLA-DPA1*01:03-DPB1*04:01',
'HLA-DPA1*01:03-DPB1*04:02', 'HLA-DPA1*01:03-DPB1*05:01', 'HLA-DPA1*01:03-DPB1*06:01', 'HLA-DPA1*01:03-DPB1*08:01', 'HLA-DPA1*01:03-DPB1*09:01',
'HLA-DPA1*01:03-DPB1*10:001', 'HLA-DPA1*01:03-DPB1*10:01', 'HLA-DPA1*01:03-DPB1*10:101', 'HLA-DPA1*01:03-DPB1*10:201', 'HLA-DPA1*01:03-DPB1*10:301',
'HLA-DPA1*01:03-DPB1*10:401', 'HLA-DPA1*01:03-DPB1*10:501', 'HLA-DPA1*01:03-DPB1*10:601', 'HLA-DPA1*01:03-DPB1*10:701', 'HLA-DPA1*01:03-DPB1*10:801',
'HLA-DPA1*01:03-DPB1*10:901', 'HLA-DPA1*01:03-DPB1*11:001', 'HLA-DPA1*01:03-DPB1*11:01', 'HLA-DPA1*01:03-DPB1*11:101', 'HLA-DPA1*01:03-DPB1*11:201',
'HLA-DPA1*01:03-DPB1*11:301', 'HLA-DPA1*01:03-DPB1*11:401', 'HLA-DPA1*01:03-DPB1*11:501', 'HLA-DPA1*01:03-DPB1*11:601', 'HLA-DPA1*01:03-DPB1*11:701',
'HLA-DPA1*01:03-DPB1*11:801', 'HLA-DPA1*01:03-DPB1*11:901', 'HLA-DPA1*01:03-DPB1*12:101', 'HLA-DPA1*01:03-DPB1*12:201', 'HLA-DPA1*01:03-DPB1*12:301',
'HLA-DPA1*01:03-DPB1*12:401', 'HLA-DPA1*01:03-DPB1*12:501', 'HLA-DPA1*01:03-DPB1*12:601', 'HLA-DPA1*01:03-DPB1*12:701', 'HLA-DPA1*01:03-DPB1*12:801',
'HLA-DPA1*01:03-DPB1*12:901', 'HLA-DPA1*01:03-DPB1*13:001', 'HLA-DPA1*01:03-DPB1*13:01', 'HLA-DPA1*01:03-DPB1*13:101', 'HLA-DPA1*01:03-DPB1*13:201',
'HLA-DPA1*01:03-DPB1*13:301', 'HLA-DPA1*01:03-DPB1*13:401', 'HLA-DPA1*01:03-DPB1*14:01', 'HLA-DPA1*01:03-DPB1*15:01', 'HLA-DPA1*01:03-DPB1*16:01',
'HLA-DPA1*01:03-DPB1*17:01', 'HLA-DPA1*01:03-DPB1*18:01', 'HLA-DPA1*01:03-DPB1*19:01', 'HLA-DPA1*01:03-DPB1*20:01', 'HLA-DPA1*01:03-DPB1*21:01',
'HLA-DPA1*01:03-DPB1*22:01', 'HLA-DPA1*01:03-DPB1*23:01', 'HLA-DPA1*01:03-DPB1*24:01', 'HLA-DPA1*01:03-DPB1*25:01', 'HLA-DPA1*01:03-DPB1*26:01',
'HLA-DPA1*01:03-DPB1*27:01', 'HLA-DPA1*01:03-DPB1*28:01', 'HLA-DPA1*01:03-DPB1*29:01', 'HLA-DPA1*01:03-DPB1*30:01', 'HLA-DPA1*01:03-DPB1*31:01',
'HLA-DPA1*01:03-DPB1*32:01', 'HLA-DPA1*01:03-DPB1*33:01', 'HLA-DPA1*01:03-DPB1*34:01', 'HLA-DPA1*01:03-DPB1*35:01', 'HLA-DPA1*01:03-DPB1*36:01',
'HLA-DPA1*01:03-DPB1*37:01', 'HLA-DPA1*01:03-DPB1*38:01', 'HLA-DPA1*01:03-DPB1*39:01', 'HLA-DPA1*01:03-DPB1*40:01', 'HLA-DPA1*01:03-DPB1*41:01',
'HLA-DPA1*01:03-DPB1*44:01', 'HLA-DPA1*01:03-DPB1*45:01', 'HLA-DPA1*01:03-DPB1*46:01', 'HLA-DPA1*01:03-DPB1*47:01', 'HLA-DPA1*01:03-DPB1*48:01',
'HLA-DPA1*01:03-DPB1*49:01', 'HLA-DPA1*01:03-DPB1*50:01', 'HLA-DPA1*01:03-DPB1*51:01', 'HLA-DPA1*01:03-DPB1*52:01', 'HLA-DPA1*01:03-DPB1*53:01',
'HLA-DPA1*01:03-DPB1*54:01', 'HLA-DPA1*01:03-DPB1*55:01', 'HLA-DPA1*01:03-DPB1*56:01', 'HLA-DPA1*01:03-DPB1*58:01', 'HLA-DPA1*01:03-DPB1*59:01',
'HLA-DPA1*01:03-DPB1*60:01', 'HLA-DPA1*01:03-DPB1*62:01', 'HLA-DPA1*01:03-DPB1*63:01', 'HLA-DPA1*01:03-DPB1*65:01', 'HLA-DPA1*01:03-DPB1*66:01',
'HLA-DPA1*01:03-DPB1*67:01', 'HLA-DPA1*01:03-DPB1*68:01', 'HLA-DPA1*01:03-DPB1*69:01', 'HLA-DPA1*01:03-DPB1*70:01', 'HLA-DPA1*01:03-DPB1*71:01',
'HLA-DPA1*01:03-DPB1*72:01', 'HLA-DPA1*01:03-DPB1*73:01', 'HLA-DPA1*01:03-DPB1*74:01', 'HLA-DPA1*01:03-DPB1*75:01', 'HLA-DPA1*01:03-DPB1*76:01',
'HLA-DPA1*01:03-DPB1*77:01', 'HLA-DPA1*01:03-DPB1*78:01', 'HLA-DPA1*01:03-DPB1*79:01', 'HLA-DPA1*01:03-DPB1*80:01', 'HLA-DPA1*01:03-DPB1*81:01',
'HLA-DPA1*01:03-DPB1*82:01', 'HLA-DPA1*01:03-DPB1*83:01', 'HLA-DPA1*01:03-DPB1*84:01', 'HLA-DPA1*01:03-DPB1*85:01', 'HLA-DPA1*01:03-DPB1*86:01',
'HLA-DPA1*01:03-DPB1*87:01', 'HLA-DPA1*01:03-DPB1*88:01', 'HLA-DPA1*01:03-DPB1*89:01', 'HLA-DPA1*01:03-DPB1*90:01', 'HLA-DPA1*01:03-DPB1*91:01',
'HLA-DPA1*01:03-DPB1*92:01', 'HLA-DPA1*01:03-DPB1*93:01', 'HLA-DPA1*01:03-DPB1*94:01', 'HLA-DPA1*01:03-DPB1*95:01', 'HLA-DPA1*01:03-DPB1*96:01',
'HLA-DPA1*01:03-DPB1*97:01', 'HLA-DPA1*01:03-DPB1*98:01', 'HLA-DPA1*01:03-DPB1*99:01', 'HLA-DPA1*01:04-DPB1*01:01', 'HLA-DPA1*01:04-DPB1*02:01',
'HLA-DPA1*01:04-DPB1*02:02', 'HLA-DPA1*01:04-DPB1*03:01', 'HLA-DPA1*01:04-DPB1*04:01', 'HLA-DPA1*01:04-DPB1*04:02', 'HLA-DPA1*01:04-DPB1*05:01',
'HLA-DPA1*01:04-DPB1*06:01', 'HLA-DPA1*01:04-DPB1*08:01', 'HLA-DPA1*01:04-DPB1*09:01', 'HLA-DPA1*01:04-DPB1*10:001', 'HLA-DPA1*01:04-DPB1*10:01',
'HLA-DPA1*01:04-DPB1*10:101', 'HLA-DPA1*01:04-DPB1*10:201', 'HLA-DPA1*01:04-DPB1*10:301', 'HLA-DPA1*01:04-DPB1*10:401', 'HLA-DPA1*01:04-DPB1*10:501',
'HLA-DPA1*01:04-DPB1*10:601', 'HLA-DPA1*01:04-DPB1*10:701', 'HLA-DPA1*01:04-DPB1*10:801', 'HLA-DPA1*01:04-DPB1*10:901', 'HLA-DPA1*01:04-DPB1*11:001',
'HLA-DPA1*01:04-DPB1*11:01', 'HLA-DPA1*01:04-DPB1*11:101', 'HLA-DPA1*01:04-DPB1*11:201', 'HLA-DPA1*01:04-DPB1*11:301', 'HLA-DPA1*01:04-DPB1*11:401',
'HLA-DPA1*01:04-DPB1*11:501', 'HLA-DPA1*01:04-DPB1*11:601', 'HLA-DPA1*01:04-DPB1*11:701', 'HLA-DPA1*01:04-DPB1*11:801', 'HLA-DPA1*01:04-DPB1*11:901',
'HLA-DPA1*01:04-DPB1*12:101', 'HLA-DPA1*01:04-DPB1*12:201', 'HLA-DPA1*01:04-DPB1*12:301', 'HLA-DPA1*01:04-DPB1*12:401', 'HLA-DPA1*01:04-DPB1*12:501',
'HLA-DPA1*01:04-DPB1*12:601', 'HLA-DPA1*01:04-DPB1*12:701', 'HLA-DPA1*01:04-DPB1*12:801', 'HLA-DPA1*01:04-DPB1*12:901', 'HLA-DPA1*01:04-DPB1*13:001',
'HLA-DPA1*01:04-DPB1*13:01', 'HLA-DPA1*01:04-DPB1*13:101', 'HLA-DPA1*01:04-DPB1*13:201', 'HLA-DPA1*01:04-DPB1*13:301', 'HLA-DPA1*01:04-DPB1*13:401',
'HLA-DPA1*01:04-DPB1*14:01', 'HLA-DPA1*01:04-DPB1*15:01', 'HLA-DPA1*01:04-DPB1*16:01', 'HLA-DPA1*01:04-DPB1*17:01', 'HLA-DPA1*01:04-DPB1*18:01',
'HLA-DPA1*01:04-DPB1*19:01', 'HLA-DPA1*01:04-DPB1*20:01', 'HLA-DPA1*01:04-DPB1*21:01', 'HLA-DPA1*01:04-DPB1*22:01', 'HLA-DPA1*01:04-DPB1*23:01',
'HLA-DPA1*01:04-DPB1*24:01', 'HLA-DPA1*01:04-DPB1*25:01', 'HLA-DPA1*01:04-DPB1*26:01', 'HLA-DPA1*01:04-DPB1*27:01', 'HLA-DPA1*01:04-DPB1*28:01',
'HLA-DPA1*01:04-DPB1*29:01', 'HLA-DPA1*01:04-DPB1*30:01', 'HLA-DPA1*01:04-DPB1*31:01', 'HLA-DPA1*01:04-DPB1*32:01', 'HLA-DPA1*01:04-DPB1*33:01',
'HLA-DPA1*01:04-DPB1*34:01', 'HLA-DPA1*01:04-DPB1*35:01', 'HLA-DPA1*01:04-DPB1*36:01', 'HLA-DPA1*01:04-DPB1*37:01', 'HLA-DPA1*01:04-DPB1*38:01',
'HLA-DPA1*01:04-DPB1*39:01', 'HLA-DPA1*01:04-DPB1*40:01', 'HLA-DPA1*01:04-DPB1*41:01', 'HLA-DPA1*01:04-DPB1*44:01', 'HLA-DPA1*01:04-DPB1*45:01',
'HLA-DPA1*01:04-DPB1*46:01', 'HLA-DPA1*01:04-DPB1*47:01', 'HLA-DPA1*01:04-DPB1*48:01', 'HLA-DPA1*01:04-DPB1*49:01', 'HLA-DPA1*01:04-DPB1*50:01',
'HLA-DPA1*01:04-DPB1*51:01', 'HLA-DPA1*01:04-DPB1*52:01', 'HLA-DPA1*01:04-DPB1*53:01', 'HLA-DPA1*01:04-DPB1*54:01', 'HLA-DPA1*01:04-DPB1*55:01',
'HLA-DPA1*01:04-DPB1*56:01', 'HLA-DPA1*01:04-DPB1*58:01', 'HLA-DPA1*01:04-DPB1*59:01', 'HLA-DPA1*01:04-DPB1*60:01', 'HLA-DPA1*01:04-DPB1*62:01',
'HLA-DPA1*01:04-DPB1*63:01', 'HLA-DPA1*01:04-DPB1*65:01', 'HLA-DPA1*01:04-DPB1*66:01', 'HLA-DPA1*01:04-DPB1*67:01', 'HLA-DPA1*01:04-DPB1*68:01',
'HLA-DPA1*01:04-DPB1*69:01', 'HLA-DPA1*01:04-DPB1*70:01', 'HLA-DPA1*01:04-DPB1*71:01', 'HLA-DPA1*01:04-DPB1*72:01', 'HLA-DPA1*01:04-DPB1*73:01',
'HLA-DPA1*01:04-DPB1*74:01', 'HLA-DPA1*01:04-DPB1*75:01', 'HLA-DPA1*01:04-DPB1*76:01', 'HLA-DPA1*01:04-DPB1*77:01', 'HLA-DPA1*01:04-DPB1*78:01',
'HLA-DPA1*01:04-DPB1*79:01', 'HLA-DPA1*01:04-DPB1*80:01', 'HLA-DPA1*01:04-DPB1*81:01', 'HLA-DPA1*01:04-DPB1*82:01', 'HLA-DPA1*01:04-DPB1*83:01',
'HLA-DPA1*01:04-DPB1*84:01', 'HLA-DPA1*01:04-DPB1*85:01', 'HLA-DPA1*01:04-DPB1*86:01', 'HLA-DPA1*01:04-DPB1*87:01', 'HLA-DPA1*01:04-DPB1*88:01',
'HLA-DPA1*01:04-DPB1*89:01', 'HLA-DPA1*01:04-DPB1*90:01', 'HLA-DPA1*01:04-DPB1*91:01', 'HLA-DPA1*01:04-DPB1*92:01', 'HLA-DPA1*01:04-DPB1*93:01',
'HLA-DPA1*01:04-DPB1*94:01', 'HLA-DPA1*01:04-DPB1*95:01', 'HLA-DPA1*01:04-DPB1*96:01', 'HLA-DPA1*01:04-DPB1*97:01', 'HLA-DPA1*01:04-DPB1*98:01',
'HLA-DPA1*01:04-DPB1*99:01', 'HLA-DPA1*01:05-DPB1*01:01', 'HLA-DPA1*01:05-DPB1*02:01', 'HLA-DPA1*01:05-DPB1*02:02', 'HLA-DPA1*01:05-DPB1*03:01',
'HLA-DPA1*01:05-DPB1*04:01', 'HLA-DPA1*01:05-DPB1*04:02', 'HLA-DPA1*01:05-DPB1*05:01', 'HLA-DPA1*01:05-DPB1*06:01', 'HLA-DPA1*01:05-DPB1*08:01',
'HLA-DPA1*01:05-DPB1*09:01', 'HLA-DPA1*01:05-DPB1*10:001', 'HLA-DPA1*01:05-DPB1*10:01', 'HLA-DPA1*01:05-DPB1*10:101', 'HLA-DPA1*01:05-DPB1*10:201',
'HLA-DPA1*01:05-DPB1*10:301', 'HLA-DPA1*01:05-DPB1*10:401', 'HLA-DPA1*01:05-DPB1*10:501', 'HLA-DPA1*01:05-DPB1*10:601', 'HLA-DPA1*01:05-DPB1*10:701',
'HLA-DPA1*01:05-DPB1*10:801', 'HLA-DPA1*01:05-DPB1*10:901', 'HLA-DPA1*01:05-DPB1*11:001', 'HLA-DPA1*01:05-DPB1*11:01', 'HLA-DPA1*01:05-DPB1*11:101',
'HLA-DPA1*01:05-DPB1*11:201', 'HLA-DPA1*01:05-DPB1*11:301', 'HLA-DPA1*01:05-DPB1*11:401', 'HLA-DPA1*01:05-DPB1*11:501', 'HLA-DPA1*01:05-DPB1*11:601',
'HLA-DPA1*01:05-DPB1*11:701', 'HLA-DPA1*01:05-DPB1*11:801', 'HLA-DPA1*01:05-DPB1*11:901', 'HLA-DPA1*01:05-DPB1*12:101', 'HLA-DPA1*01:05-DPB1*12:201',
'HLA-DPA1*01:05-DPB1*12:301', 'HLA-DPA1*01:05-DPB1*12:401', 'HLA-DPA1*01:05-DPB1*12:501', 'HLA-DPA1*01:05-DPB1*12:601', 'HLA-DPA1*01:05-DPB1*12:701',
'HLA-DPA1*01:05-DPB1*12:801', 'HLA-DPA1*01:05-DPB1*12:901', 'HLA-DPA1*01:05-DPB1*13:001', 'HLA-DPA1*01:05-DPB1*13:01', 'HLA-DPA1*01:05-DPB1*13:101',
'HLA-DPA1*01:05-DPB1*13:201', 'HLA-DPA1*01:05-DPB1*13:301', 'HLA-DPA1*01:05-DPB1*13:401', 'HLA-DPA1*01:05-DPB1*14:01', 'HLA-DPA1*01:05-DPB1*15:01',
'HLA-DPA1*01:05-DPB1*16:01', 'HLA-DPA1*01:05-DPB1*17:01', 'HLA-DPA1*01:05-DPB1*18:01', 'HLA-DPA1*01:05-DPB1*19:01', 'HLA-DPA1*01:05-DPB1*20:01',
'HLA-DPA1*01:05-DPB1*21:01', 'HLA-DPA1*01:05-DPB1*22:01', 'HLA-DPA1*01:05-DPB1*23:01', 'HLA-DPA1*01:05-DPB1*24:01', 'HLA-DPA1*01:05-DPB1*25:01',
'HLA-DPA1*01:05-DPB1*26:01', 'HLA-DPA1*01:05-DPB1*27:01', 'HLA-DPA1*01:05-DPB1*28:01', 'HLA-DPA1*01:05-DPB1*29:01', 'HLA-DPA1*01:05-DPB1*30:01',
'HLA-DPA1*01:05-DPB1*31:01', 'HLA-DPA1*01:05-DPB1*32:01', 'HLA-DPA1*01:05-DPB1*33:01', 'HLA-DPA1*01:05-DPB1*34:01', 'HLA-DPA1*01:05-DPB1*35:01',
'HLA-DPA1*01:05-DPB1*36:01', 'HLA-DPA1*01:05-DPB1*37:01', 'HLA-DPA1*01:05-DPB1*38:01', 'HLA-DPA1*01:05-DPB1*39:01', 'HLA-DPA1*01:05-DPB1*40:01',
'HLA-DPA1*01:05-DPB1*41:01', 'HLA-DPA1*01:05-DPB1*44:01', 'HLA-DPA1*01:05-DPB1*45:01', 'HLA-DPA1*01:05-DPB1*46:01', 'HLA-DPA1*01:05-DPB1*47:01',
'HLA-DPA1*01:05-DPB1*48:01', 'HLA-DPA1*01:05-DPB1*49:01', 'HLA-DPA1*01:05-DPB1*50:01', 'HLA-DPA1*01:05-DPB1*51:01', 'HLA-DPA1*01:05-DPB1*52:01',
'HLA-DPA1*01:05-DPB1*53:01', 'HLA-DPA1*01:05-DPB1*54:01', 'HLA-DPA1*01:05-DPB1*55:01', 'HLA-DPA1*01:05-DPB1*56:01', 'HLA-DPA1*01:05-DPB1*58:01',
'HLA-DPA1*01:05-DPB1*59:01', 'HLA-DPA1*01:05-DPB1*60:01', 'HLA-DPA1*01:05-DPB1*62:01', 'HLA-DPA1*01:05-DPB1*63:01', 'HLA-DPA1*01:05-DPB1*65:01',
'HLA-DPA1*01:05-DPB1*66:01', 'HLA-DPA1*01:05-DPB1*67:01', 'HLA-DPA1*01:05-DPB1*68:01', 'HLA-DPA1*01:05-DPB1*69:01', 'HLA-DPA1*01:05-DPB1*70:01',
'HLA-DPA1*01:05-DPB1*71:01', 'HLA-DPA1*01:05-DPB1*72:01', 'HLA-DPA1*01:05-DPB1*73:01', 'HLA-DPA1*01:05-DPB1*74:01', 'HLA-DPA1*01:05-DPB1*75:01',
'HLA-DPA1*01:05-DPB1*76:01', 'HLA-DPA1*01:05-DPB1*77:01', 'HLA-DPA1*01:05-DPB1*78:01', 'HLA-DPA1*01:05-DPB1*79:01', 'HLA-DPA1*01:05-DPB1*80:01',
'HLA-DPA1*01:05-DPB1*81:01', 'HLA-DPA1*01:05-DPB1*82:01', 'HLA-DPA1*01:05-DPB1*83:01', 'HLA-DPA1*01:05-DPB1*84:01', 'HLA-DPA1*01:05-DPB1*85:01',
'HLA-DPA1*01:05-DPB1*86:01', 'HLA-DPA1*01:05-DPB1*87:01', 'HLA-DPA1*01:05-DPB1*88:01', 'HLA-DPA1*01:05-DPB1*89:01', 'HLA-DPA1*01:05-DPB1*90:01',
'HLA-DPA1*01:05-DPB1*91:01', 'HLA-DPA1*01:05-DPB1*92:01', 'HLA-DPA1*01:05-DPB1*93:01', 'HLA-DPA1*01:05-DPB1*94:01', 'HLA-DPA1*01:05-DPB1*95:01',
'HLA-DPA1*01:05-DPB1*96:01', 'HLA-DPA1*01:05-DPB1*97:01', 'HLA-DPA1*01:05-DPB1*98:01', 'HLA-DPA1*01:05-DPB1*99:01', 'HLA-DPA1*01:06-DPB1*01:01',
'HLA-DPA1*01:06-DPB1*02:01', 'HLA-DPA1*01:06-DPB1*02:02', 'HLA-DPA1*01:06-DPB1*03:01', 'HLA-DPA1*01:06-DPB1*04:01', 'HLA-DPA1*01:06-DPB1*04:02',
'HLA-DPA1*01:06-DPB1*05:01', 'HLA-DPA1*01:06-DPB1*06:01', 'HLA-DPA1*01:06-DPB1*08:01', 'HLA-DPA1*01:06-DPB1*09:01', 'HLA-DPA1*01:06-DPB1*10:001',
'HLA-DPA1*01:06-DPB1*10:01', 'HLA-DPA1*01:06-DPB1*10:101', 'HLA-DPA1*01:06-DPB1*10:201', 'HLA-DPA1*01:06-DPB1*10:301', 'HLA-DPA1*01:06-DPB1*10:401',
'HLA-DPA1*01:06-DPB1*10:501', 'HLA-DPA1*01:06-DPB1*10:601', 'HLA-DPA1*01:06-DPB1*10:701', 'HLA-DPA1*01:06-DPB1*10:801', 'HLA-DPA1*01:06-DPB1*10:901',
'HLA-DPA1*01:06-DPB1*11:001', 'HLA-DPA1*01:06-DPB1*11:01', 'HLA-DPA1*01:06-DPB1*11:101', 'HLA-DPA1*01:06-DPB1*11:201', 'HLA-DPA1*01:06-DPB1*11:301',
'HLA-DPA1*01:06-DPB1*11:401', 'HLA-DPA1*01:06-DPB1*11:501', 'HLA-DPA1*01:06-DPB1*11:601', 'HLA-DPA1*01:06-DPB1*11:701', 'HLA-DPA1*01:06-DPB1*11:801',
'HLA-DPA1*01:06-DPB1*11:901', 'HLA-DPA1*01:06-DPB1*12:101', 'HLA-DPA1*01:06-DPB1*12:201', 'HLA-DPA1*01:06-DPB1*12:301', 'HLA-DPA1*01:06-DPB1*12:401',
'HLA-DPA1*01:06-DPB1*12:501', 'HLA-DPA1*01:06-DPB1*12:601', 'HLA-DPA1*01:06-DPB1*12:701', 'HLA-DPA1*01:06-DPB1*12:801', 'HLA-DPA1*01:06-DPB1*12:901',
'HLA-DPA1*01:06-DPB1*13:001', 'HLA-DPA1*01:06-DPB1*13:01', 'HLA-DPA1*01:06-DPB1*13:101', 'HLA-DPA1*01:06-DPB1*13:201', 'HLA-DPA1*01:06-DPB1*13:301',
'HLA-DPA1*01:06-DPB1*13:401', 'HLA-DPA1*01:06-DPB1*14:01', 'HLA-DPA1*01:06-DPB1*15:01', 'HLA-DPA1*01:06-DPB1*16:01', 'HLA-DPA1*01:06-DPB1*17:01',
'HLA-DPA1*01:06-DPB1*18:01', 'HLA-DPA1*01:06-DPB1*19:01', 'HLA-DPA1*01:06-DPB1*20:01', 'HLA-DPA1*01:06-DPB1*21:01', 'HLA-DPA1*01:06-DPB1*22:01',
'HLA-DPA1*01:06-DPB1*23:01', 'HLA-DPA1*01:06-DPB1*24:01', 'HLA-DPA1*01:06-DPB1*25:01', 'HLA-DPA1*01:06-DPB1*26:01', 'HLA-DPA1*01:06-DPB1*27:01',
'HLA-DPA1*01:06-DPB1*28:01', 'HLA-DPA1*01:06-DPB1*29:01', 'HLA-DPA1*01:06-DPB1*30:01', 'HLA-DPA1*01:06-DPB1*31:01', 'HLA-DPA1*01:06-DPB1*32:01',
'HLA-DPA1*01:06-DPB1*33:01', 'HLA-DPA1*01:06-DPB1*34:01', 'HLA-DPA1*01:06-DPB1*35:01', 'HLA-DPA1*01:06-DPB1*36:01', 'HLA-DPA1*01:06-DPB1*37:01',
'HLA-DPA1*01:06-DPB1*38:01', 'HLA-DPA1*01:06-DPB1*39:01', 'HLA-DPA1*01:06-DPB1*40:01', 'HLA-DPA1*01:06-DPB1*41:01', 'HLA-DPA1*01:06-DPB1*44:01',
'HLA-DPA1*01:06-DPB1*45:01', 'HLA-DPA1*01:06-DPB1*46:01', 'HLA-DPA1*01:06-DPB1*47:01', 'HLA-DPA1*01:06-DPB1*48:01', 'HLA-DPA1*01:06-DPB1*49:01',
'HLA-DPA1*01:06-DPB1*50:01', 'HLA-DPA1*01:06-DPB1*51:01', 'HLA-DPA1*01:06-DPB1*52:01', 'HLA-DPA1*01:06-DPB1*53:01', 'HLA-DPA1*01:06-DPB1*54:01',
'HLA-DPA1*01:06-DPB1*55:01', 'HLA-DPA1*01:06-DPB1*56:01', 'HLA-DPA1*01:06-DPB1*58:01', 'HLA-DPA1*01:06-DPB1*59:01', 'HLA-DPA1*01:06-DPB1*60:01',
'HLA-DPA1*01:06-DPB1*62:01', 'HLA-DPA1*01:06-DPB1*63:01', 'HLA-DPA1*01:06-DPB1*65:01', 'HLA-DPA1*01:06-DPB1*66:01', 'HLA-DPA1*01:06-DPB1*67:01',
'HLA-DPA1*01:06-DPB1*68:01', 'HLA-DPA1*01:06-DPB1*69:01', 'HLA-DPA1*01:06-DPB1*70:01', 'HLA-DPA1*01:06-DPB1*71:01', 'HLA-DPA1*01:06-DPB1*72:01',
'HLA-DPA1*01:06-DPB1*73:01', 'HLA-DPA1*01:06-DPB1*74:01', 'HLA-DPA1*01:06-DPB1*75:01', 'HLA-DPA1*01:06-DPB1*76:01', 'HLA-DPA1*01:06-DPB1*77:01',
'HLA-DPA1*01:06-DPB1*78:01', 'HLA-DPA1*01:06-DPB1*79:01', 'HLA-DPA1*01:06-DPB1*80:01', 'HLA-DPA1*01:06-DPB1*81:01', 'HLA-DPA1*01:06-DPB1*82:01',
'HLA-DPA1*01:06-DPB1*83:01', 'HLA-DPA1*01:06-DPB1*84:01', 'HLA-DPA1*01:06-DPB1*85:01', 'HLA-DPA1*01:06-DPB1*86:01', 'HLA-DPA1*01:06-DPB1*87:01',
'HLA-DPA1*01:06-DPB1*88:01', 'HLA-DPA1*01:06-DPB1*89:01', 'HLA-DPA1*01:06-DPB1*90:01', 'HLA-DPA1*01:06-DPB1*91:01', 'HLA-DPA1*01:06-DPB1*92:01',
'HLA-DPA1*01:06-DPB1*93:01', 'HLA-DPA1*01:06-DPB1*94:01', 'HLA-DPA1*01:06-DPB1*95:01', 'HLA-DPA1*01:06-DPB1*96:01', 'HLA-DPA1*01:06-DPB1*97:01',
'HLA-DPA1*01:06-DPB1*98:01', 'HLA-DPA1*01:06-DPB1*99:01', 'HLA-DPA1*01:07-DPB1*01:01', 'HLA-DPA1*01:07-DPB1*02:01', 'HLA-DPA1*01:07-DPB1*02:02',
'HLA-DPA1*01:07-DPB1*03:01', 'HLA-DPA1*01:07-DPB1*04:01', 'HLA-DPA1*01:07-DPB1*04:02', 'HLA-DPA1*01:07-DPB1*05:01', 'HLA-DPA1*01:07-DPB1*06:01',
'HLA-DPA1*01:07-DPB1*08:01', 'HLA-DPA1*01:07-DPB1*09:01', 'HLA-DPA1*01:07-DPB1*10:001', 'HLA-DPA1*01:07-DPB1*10:01', 'HLA-DPA1*01:07-DPB1*10:101',
'HLA-DPA1*01:07-DPB1*10:201', 'HLA-DPA1*01:07-DPB1*10:301', 'HLA-DPA1*01:07-DPB1*10:401', 'HLA-DPA1*01:07-DPB1*10:501', 'HLA-DPA1*01:07-DPB1*10:601',
'HLA-DPA1*01:07-DPB1*10:701', 'HLA-DPA1*01:07-DPB1*10:801', 'HLA-DPA1*01:07-DPB1*10:901', 'HLA-DPA1*01:07-DPB1*11:001', 'HLA-DPA1*01:07-DPB1*11:01',
'HLA-DPA1*01:07-DPB1*11:101', 'HLA-DPA1*01:07-DPB1*11:201', 'HLA-DPA1*01:07-DPB1*11:301', 'HLA-DPA1*01:07-DPB1*11:401', 'HLA-DPA1*01:07-DPB1*11:501',
'HLA-DPA1*01:07-DPB1*11:601', 'HLA-DPA1*01:07-DPB1*11:701', 'HLA-DPA1*01:07-DPB1*11:801', 'HLA-DPA1*01:07-DPB1*11:901', 'HLA-DPA1*01:07-DPB1*12:101',
'HLA-DPA1*01:07-DPB1*12:201', 'HLA-DPA1*01:07-DPB1*12:301', 'HLA-DPA1*01:07-DPB1*12:401', 'HLA-DPA1*01:07-DPB1*12:501', 'HLA-DPA1*01:07-DPB1*12:601',
'HLA-DPA1*01:07-DPB1*12:701', 'HLA-DPA1*01:07-DPB1*12:801', 'HLA-DPA1*01:07-DPB1*12:901', 'HLA-DPA1*01:07-DPB1*13:001', 'HLA-DPA1*01:07-DPB1*13:01',
'HLA-DPA1*01:07-DPB1*13:101', 'HLA-DPA1*01:07-DPB1*13:201', 'HLA-DPA1*01:07-DPB1*13:301', 'HLA-DPA1*01:07-DPB1*13:401', 'HLA-DPA1*01:07-DPB1*14:01',
'HLA-DPA1*01:07-DPB1*15:01', 'HLA-DPA1*01:07-DPB1*16:01', 'HLA-DPA1*01:07-DPB1*17:01', 'HLA-DPA1*01:07-DPB1*18:01', 'HLA-DPA1*01:07-DPB1*19:01',
'HLA-DPA1*01:07-DPB1*20:01', 'HLA-DPA1*01:07-DPB1*21:01', 'HLA-DPA1*01:07-DPB1*22:01', 'HLA-DPA1*01:07-DPB1*23:01', 'HLA-DPA1*01:07-DPB1*24:01',
'HLA-DPA1*01:07-DPB1*25:01', 'HLA-DPA1*01:07-DPB1*26:01', 'HLA-DPA1*01:07-DPB1*27:01', 'HLA-DPA1*01:07-DPB1*28:01', 'HLA-DPA1*01:07-DPB1*29:01',
'HLA-DPA1*01:07-DPB1*30:01', 'HLA-DPA1*01:07-DPB1*31:01', 'HLA-DPA1*01:07-DPB1*32:01', 'HLA-DPA1*01:07-DPB1*33:01', 'HLA-DPA1*01:07-DPB1*34:01',
'HLA-DPA1*01:07-DPB1*35:01', 'HLA-DPA1*01:07-DPB1*36:01', 'HLA-DPA1*01:07-DPB1*37:01', 'HLA-DPA1*01:07-DPB1*38:01', 'HLA-DPA1*01:07-DPB1*39:01',
'HLA-DPA1*01:07-DPB1*40:01', 'HLA-DPA1*01:07-DPB1*41:01', 'HLA-DPA1*01:07-DPB1*44:01', 'HLA-DPA1*01:07-DPB1*45:01', 'HLA-DPA1*01:07-DPB1*46:01',
'HLA-DPA1*01:07-DPB1*47:01', 'HLA-DPA1*01:07-DPB1*48:01', 'HLA-DPA1*01:07-DPB1*49:01', 'HLA-DPA1*01:07-DPB1*50:01', 'HLA-DPA1*01:07-DPB1*51:01',
'HLA-DPA1*01:07-DPB1*52:01', 'HLA-DPA1*01:07-DPB1*53:01', 'HLA-DPA1*01:07-DPB1*54:01', 'HLA-DPA1*01:07-DPB1*55:01', 'HLA-DPA1*01:07-DPB1*56:01',
'HLA-DPA1*01:07-DPB1*58:01', 'HLA-DPA1*01:07-DPB1*59:01', 'HLA-DPA1*01:07-DPB1*60:01', 'HLA-DPA1*01:07-DPB1*62:01', 'HLA-DPA1*01:07-DPB1*63:01',
'HLA-DPA1*01:07-DPB1*65:01', 'HLA-DPA1*01:07-DPB1*66:01', 'HLA-DPA1*01:07-DPB1*67:01', 'HLA-DPA1*01:07-DPB1*68:01', 'HLA-DPA1*01:07-DPB1*69:01',
'HLA-DPA1*01:07-DPB1*70:01', 'HLA-DPA1*01:07-DPB1*71:01', 'HLA-DPA1*01:07-DPB1*72:01', 'HLA-DPA1*01:07-DPB1*73:01', 'HLA-DPA1*01:07-DPB1*74:01',
'HLA-DPA1*01:07-DPB1*75:01', 'HLA-DPA1*01:07-DPB1*76:01', 'HLA-DPA1*01:07-DPB1*77:01', 'HLA-DPA1*01:07-DPB1*78:01', 'HLA-DPA1*01:07-DPB1*79:01',
'HLA-DPA1*01:07-DPB1*80:01', 'HLA-DPA1*01:07-DPB1*81:01', 'HLA-DPA1*01:07-DPB1*82:01', 'HLA-DPA1*01:07-DPB1*83:01', 'HLA-DPA1*01:07-DPB1*84:01',
'HLA-DPA1*01:07-DPB1*85:01', 'HLA-DPA1*01:07-DPB1*86:01', 'HLA-DPA1*01:07-DPB1*87:01', 'HLA-DPA1*01:07-DPB1*88:01', 'HLA-DPA1*01:07-DPB1*89:01',
'HLA-DPA1*01:07-DPB1*90:01', 'HLA-DPA1*01:07-DPB1*91:01', 'HLA-DPA1*01:07-DPB1*92:01', 'HLA-DPA1*01:07-DPB1*93:01', 'HLA-DPA1*01:07-DPB1*94:01',
'HLA-DPA1*01:07-DPB1*95:01', 'HLA-DPA1*01:07-DPB1*96:01', 'HLA-DPA1*01:07-DPB1*97:01', 'HLA-DPA1*01:07-DPB1*98:01', 'HLA-DPA1*01:07-DPB1*99:01',
'HLA-DPA1*01:08-DPB1*01:01', 'HLA-DPA1*01:08-DPB1*02:01', 'HLA-DPA1*01:08-DPB1*02:02', 'HLA-DPA1*01:08-DPB1*03:01', 'HLA-DPA1*01:08-DPB1*04:01',
'HLA-DPA1*01:08-DPB1*04:02', 'HLA-DPA1*01:08-DPB1*05:01', 'HLA-DPA1*01:08-DPB1*06:01', 'HLA-DPA1*01:08-DPB1*08:01', 'HLA-DPA1*01:08-DPB1*09:01',
'HLA-DPA1*01:08-DPB1*10:001', 'HLA-DPA1*01:08-DPB1*10:01', 'HLA-DPA1*01:08-DPB1*10:101', 'HLA-DPA1*01:08-DPB1*10:201', 'HLA-DPA1*01:08-DPB1*10:301',
'HLA-DPA1*01:08-DPB1*10:401', 'HLA-DPA1*01:08-DPB1*10:501', 'HLA-DPA1*01:08-DPB1*10:601', 'HLA-DPA1*01:08-DPB1*10:701', 'HLA-DPA1*01:08-DPB1*10:801',
'HLA-DPA1*01:08-DPB1*10:901', 'HLA-DPA1*01:08-DPB1*11:001', 'HLA-DPA1*01:08-DPB1*11:01', 'HLA-DPA1*01:08-DPB1*11:101', 'HLA-DPA1*01:08-DPB1*11:201',
'HLA-DPA1*01:08-DPB1*11:301', 'HLA-DPA1*01:08-DPB1*11:401', 'HLA-DPA1*01:08-DPB1*11:501', 'HLA-DPA1*01:08-DPB1*11:601', 'HLA-DPA1*01:08-DPB1*11:701',
'HLA-DPA1*01:08-DPB1*11:801', 'HLA-DPA1*01:08-DPB1*11:901', 'HLA-DPA1*01:08-DPB1*12:101', 'HLA-DPA1*01:08-DPB1*12:201', 'HLA-DPA1*01:08-DPB1*12:301',
'HLA-DPA1*01:08-DPB1*12:401', 'HLA-DPA1*01:08-DPB1*12:501', 'HLA-DPA1*01:08-DPB1*12:601', 'HLA-DPA1*01:08-DPB1*12:701', 'HLA-DPA1*01:08-DPB1*12:801',
'HLA-DPA1*01:08-DPB1*12:901', 'HLA-DPA1*01:08-DPB1*13:001', 'HLA-DPA1*01:08-DPB1*13:01', 'HLA-DPA1*01:08-DPB1*13:101', 'HLA-DPA1*01:08-DPB1*13:201',
'HLA-DPA1*01:08-DPB1*13:301', 'HLA-DPA1*01:08-DPB1*13:401', 'HLA-DPA1*01:08-DPB1*14:01', 'HLA-DPA1*01:08-DPB1*15:01', 'HLA-DPA1*01:08-DPB1*16:01',
'HLA-DPA1*01:08-DPB1*17:01', 'HLA-DPA1*01:08-DPB1*18:01', 'HLA-DPA1*01:08-DPB1*19:01', 'HLA-DPA1*01:08-DPB1*20:01', 'HLA-DPA1*01:08-DPB1*21:01',
'HLA-DPA1*01:08-DPB1*22:01', 'HLA-DPA1*01:08-DPB1*23:01', 'HLA-DPA1*01:08-DPB1*24:01', 'HLA-DPA1*01:08-DPB1*25:01', 'HLA-DPA1*01:08-DPB1*26:01',
'HLA-DPA1*01:08-DPB1*27:01', 'HLA-DPA1*01:08-DPB1*28:01', 'HLA-DPA1*01:08-DPB1*29:01', 'HLA-DPA1*01:08-DPB1*30:01', 'HLA-DPA1*01:08-DPB1*31:01',
'HLA-DPA1*01:08-DPB1*32:01', 'HLA-DPA1*01:08-DPB1*33:01', 'HLA-DPA1*01:08-DPB1*34:01', 'HLA-DPA1*01:08-DPB1*35:01', 'HLA-DPA1*01:08-DPB1*36:01',
'HLA-DPA1*01:08-DPB1*37:01', 'HLA-DPA1*01:08-DPB1*38:01', 'HLA-DPA1*01:08-DPB1*39:01', 'HLA-DPA1*01:08-DPB1*40:01', 'HLA-DPA1*01:08-DPB1*41:01',
'HLA-DPA1*01:08-DPB1*44:01', 'HLA-DPA1*01:08-DPB1*45:01', 'HLA-DPA1*01:08-DPB1*46:01', 'HLA-DPA1*01:08-DPB1*47:01', 'HLA-DPA1*01:08-DPB1*48:01',
'HLA-DPA1*01:08-DPB1*49:01', 'HLA-DPA1*01:08-DPB1*50:01', 'HLA-DPA1*01:08-DPB1*51:01', 'HLA-DPA1*01:08-DPB1*52:01', 'HLA-DPA1*01:08-DPB1*53:01',
'HLA-DPA1*01:08-DPB1*54:01', 'HLA-DPA1*01:08-DPB1*55:01', 'HLA-DPA1*01:08-DPB1*56:01', 'HLA-DPA1*01:08-DPB1*58:01', 'HLA-DPA1*01:08-DPB1*59:01',
'HLA-DPA1*01:08-DPB1*60:01', 'HLA-DPA1*01:08-DPB1*62:01', 'HLA-DPA1*01:08-DPB1*63:01', 'HLA-DPA1*01:08-DPB1*65:01', 'HLA-DPA1*01:08-DPB1*66:01',
'HLA-DPA1*01:08-DPB1*67:01', 'HLA-DPA1*01:08-DPB1*68:01', 'HLA-DPA1*01:08-DPB1*69:01', 'HLA-DPA1*01:08-DPB1*70:01', 'HLA-DPA1*01:08-DPB1*71:01',
'HLA-DPA1*01:08-DPB1*72:01', 'HLA-DPA1*01:08-DPB1*73:01', 'HLA-DPA1*01:08-DPB1*74:01', 'HLA-DPA1*01:08-DPB1*75:01', 'HLA-DPA1*01:08-DPB1*76:01',
'HLA-DPA1*01:08-DPB1*77:01', 'HLA-DPA1*01:08-DPB1*78:01', 'HLA-DPA1*01:08-DPB1*79:01', 'HLA-DPA1*01:08-DPB1*80:01', 'HLA-DPA1*01:08-DPB1*81:01',
'HLA-DPA1*01:08-DPB1*82:01', 'HLA-DPA1*01:08-DPB1*83:01', 'HLA-DPA1*01:08-DPB1*84:01', 'HLA-DPA1*01:08-DPB1*85:01', 'HLA-DPA1*01:08-DPB1*86:01',
'HLA-DPA1*01:08-DPB1*87:01', 'HLA-DPA1*01:08-DPB1*88:01', 'HLA-DPA1*01:08-DPB1*89:01', 'HLA-DPA1*01:08-DPB1*90:01', 'HLA-DPA1*01:08-DPB1*91:01',
'HLA-DPA1*01:08-DPB1*92:01', 'HLA-DPA1*01:08-DPB1*93:01', 'HLA-DPA1*01:08-DPB1*94:01', 'HLA-DPA1*01:08-DPB1*95:01', 'HLA-DPA1*01:08-DPB1*96:01',
'HLA-DPA1*01:08-DPB1*97:01', 'HLA-DPA1*01:08-DPB1*98:01', 'HLA-DPA1*01:08-DPB1*99:01', 'HLA-DPA1*01:09-DPB1*01:01', 'HLA-DPA1*01:09-DPB1*02:01',
'HLA-DPA1*01:09-DPB1*02:02', 'HLA-DPA1*01:09-DPB1*03:01', 'HLA-DPA1*01:09-DPB1*04:01', 'HLA-DPA1*01:09-DPB1*04:02', 'HLA-DPA1*01:09-DPB1*05:01',
'HLA-DPA1*01:09-DPB1*06:01', 'HLA-DPA1*01:09-DPB1*08:01', 'HLA-DPA1*01:09-DPB1*09:01', 'HLA-DPA1*01:09-DPB1*10:001', 'HLA-DPA1*01:09-DPB1*10:01',
'HLA-DPA1*01:09-DPB1*10:101', 'HLA-DPA1*01:09-DPB1*10:201', 'HLA-DPA1*01:09-DPB1*10:301', 'HLA-DPA1*01:09-DPB1*10:401', 'HLA-DPA1*01:09-DPB1*10:501',
'HLA-DPA1*01:09-DPB1*10:601', 'HLA-DPA1*01:09-DPB1*10:701', 'HLA-DPA1*01:09-DPB1*10:801', 'HLA-DPA1*01:09-DPB1*10:901', 'HLA-DPA1*01:09-DPB1*11:001',
'HLA-DPA1*01:09-DPB1*11:01', 'HLA-DPA1*01:09-DPB1*11:101', 'HLA-DPA1*01:09-DPB1*11:201', 'HLA-DPA1*01:09-DPB1*11:301', 'HLA-DPA1*01:09-DPB1*11:401',
'HLA-DPA1*01:09-DPB1*11:501', 'HLA-DPA1*01:09-DPB1*11:601', 'HLA-DPA1*01:09-DPB1*11:701', 'HLA-DPA1*01:09-DPB1*11:801', 'HLA-DPA1*01:09-DPB1*11:901',
'HLA-DPA1*01:09-DPB1*12:101', 'HLA-DPA1*01:09-DPB1*12:201', 'HLA-DPA1*01:09-DPB1*12:301', 'HLA-DPA1*01:09-DPB1*12:401', 'HLA-DPA1*01:09-DPB1*12:501',
'HLA-DPA1*01:09-DPB1*12:601', 'HLA-DPA1*01:09-DPB1*12:701', 'HLA-DPA1*01:09-DPB1*12:801', 'HLA-DPA1*01:09-DPB1*12:901', 'HLA-DPA1*01:09-DPB1*13:001',
'HLA-DPA1*01:09-DPB1*13:01', 'HLA-DPA1*01:09-DPB1*13:101', 'HLA-DPA1*01:09-DPB1*13:201', 'HLA-DPA1*01:09-DPB1*13:301', 'HLA-DPA1*01:09-DPB1*13:401',
'HLA-DPA1*01:09-DPB1*14:01', 'HLA-DPA1*01:09-DPB1*15:01', 'HLA-DPA1*01:09-DPB1*16:01', 'HLA-DPA1*01:09-DPB1*17:01', 'HLA-DPA1*01:09-DPB1*18:01',
'HLA-DPA1*01:09-DPB1*19:01', 'HLA-DPA1*01:09-DPB1*20:01', 'HLA-DPA1*01:09-DPB1*21:01', 'HLA-DPA1*01:09-DPB1*22:01', 'HLA-DPA1*01:09-DPB1*23:01',
'HLA-DPA1*01:09-DPB1*24:01', 'HLA-DPA1*01:09-DPB1*25:01', 'HLA-DPA1*01:09-DPB1*26:01', 'HLA-DPA1*01:09-DPB1*27:01', 'HLA-DPA1*01:09-DPB1*28:01',
'HLA-DPA1*01:09-DPB1*29:01', 'HLA-DPA1*01:09-DPB1*30:01', 'HLA-DPA1*01:09-DPB1*31:01', 'HLA-DPA1*01:09-DPB1*32:01', 'HLA-DPA1*01:09-DPB1*33:01',
'HLA-DPA1*01:09-DPB1*34:01', 'HLA-DPA1*01:09-DPB1*35:01', 'HLA-DPA1*01:09-DPB1*36:01', 'HLA-DPA1*01:09-DPB1*37:01', 'HLA-DPA1*01:09-DPB1*38:01',
'HLA-DPA1*01:09-DPB1*39:01', 'HLA-DPA1*01:09-DPB1*40:01', 'HLA-DPA1*01:09-DPB1*41:01', 'HLA-DPA1*01:09-DPB1*44:01', 'HLA-DPA1*01:09-DPB1*45:01',
'HLA-DPA1*01:09-DPB1*46:01', 'HLA-DPA1*01:09-DPB1*47:01', 'HLA-DPA1*01:09-DPB1*48:01', 'HLA-DPA1*01:09-DPB1*49:01', 'HLA-DPA1*01:09-DPB1*50:01',
'HLA-DPA1*01:09-DPB1*51:01', 'HLA-DPA1*01:09-DPB1*52:01', 'HLA-DPA1*01:09-DPB1*53:01', 'HLA-DPA1*01:09-DPB1*54:01', 'HLA-DPA1*01:09-DPB1*55:01',
'HLA-DPA1*01:09-DPB1*56:01', 'HLA-DPA1*01:09-DPB1*58:01', 'HLA-DPA1*01:09-DPB1*59:01', 'HLA-DPA1*01:09-DPB1*60:01', 'HLA-DPA1*01:09-DPB1*62:01',
'HLA-DPA1*01:09-DPB1*63:01', 'HLA-DPA1*01:09-DPB1*65:01', 'HLA-DPA1*01:09-DPB1*66:01', 'HLA-DPA1*01:09-DPB1*67:01', 'HLA-DPA1*01:09-DPB1*68:01',
'HLA-DPA1*01:09-DPB1*69:01', 'HLA-DPA1*01:09-DPB1*70:01', 'HLA-DPA1*01:09-DPB1*71:01', 'HLA-DPA1*01:09-DPB1*72:01', 'HLA-DPA1*01:09-DPB1*73:01',
'HLA-DPA1*01:09-DPB1*74:01', 'HLA-DPA1*01:09-DPB1*75:01', 'HLA-DPA1*01:09-DPB1*76:01', 'HLA-DPA1*01:09-DPB1*77:01', 'HLA-DPA1*01:09-DPB1*78:01',
'HLA-DPA1*01:09-DPB1*79:01', 'HLA-DPA1*01:09-DPB1*80:01', 'HLA-DPA1*01:09-DPB1*81:01', 'HLA-DPA1*01:09-DPB1*82:01', 'HLA-DPA1*01:09-DPB1*83:01',
'HLA-DPA1*01:09-DPB1*84:01', 'HLA-DPA1*01:09-DPB1*85:01', 'HLA-DPA1*01:09-DPB1*86:01', 'HLA-DPA1*01:09-DPB1*87:01', 'HLA-DPA1*01:09-DPB1*88:01',
'HLA-DPA1*01:09-DPB1*89:01', 'HLA-DPA1*01:09-DPB1*90:01', 'HLA-DPA1*01:09-DPB1*91:01', 'HLA-DPA1*01:09-DPB1*92:01', 'HLA-DPA1*01:09-DPB1*93:01',
'HLA-DPA1*01:09-DPB1*94:01', 'HLA-DPA1*01:09-DPB1*95:01', 'HLA-DPA1*01:09-DPB1*96:01', 'HLA-DPA1*01:09-DPB1*97:01', 'HLA-DPA1*01:09-DPB1*98:01',
'HLA-DPA1*01:09-DPB1*99:01', 'HLA-DPA1*01:10-DPB1*01:01', 'HLA-DPA1*01:10-DPB1*02:01', 'HLA-DPA1*01:10-DPB1*02:02', 'HLA-DPA1*01:10-DPB1*03:01',
'HLA-DPA1*01:10-DPB1*04:01', 'HLA-DPA1*01:10-DPB1*04:02', 'HLA-DPA1*01:10-DPB1*05:01', 'HLA-DPA1*01:10-DPB1*06:01', 'HLA-DPA1*01:10-DPB1*08:01',
'HLA-DPA1*01:10-DPB1*09:01', 'HLA-DPA1*01:10-DPB1*10:001', 'HLA-DPA1*01:10-DPB1*10:01', 'HLA-DPA1*01:10-DPB1*10:101', 'HLA-DPA1*01:10-DPB1*10:201',
'HLA-DPA1*01:10-DPB1*10:301', 'HLA-DPA1*01:10-DPB1*10:401', 'HLA-DPA1*01:10-DPB1*10:501', 'HLA-DPA1*01:10-DPB1*10:601', 'HLA-DPA1*01:10-DPB1*10:701',
'HLA-DPA1*01:10-DPB1*10:801', 'HLA-DPA1*01:10-DPB1*10:901', 'HLA-DPA1*01:10-DPB1*11:001', 'HLA-DPA1*01:10-DPB1*11:01', 'HLA-DPA1*01:10-DPB1*11:101',
'HLA-DPA1*01:10-DPB1*11:201', 'HLA-DPA1*01:10-DPB1*11:301', 'HLA-DPA1*01:10-DPB1*11:401', 'HLA-DPA1*01:10-DPB1*11:501', 'HLA-DPA1*01:10-DPB1*11:601',
'HLA-DPA1*01:10-DPB1*11:701', 'HLA-DPA1*01:10-DPB1*11:801', 'HLA-DPA1*01:10-DPB1*11:901', 'HLA-DPA1*01:10-DPB1*12:101', 'HLA-DPA1*01:10-DPB1*12:201',
'HLA-DPA1*01:10-DPB1*12:301', 'HLA-DPA1*01:10-DPB1*12:401', 'HLA-DPA1*01:10-DPB1*12:501', 'HLA-DPA1*01:10-DPB1*12:601', 'HLA-DPA1*01:10-DPB1*12:701',
'HLA-DPA1*01:10-DPB1*12:801', 'HLA-DPA1*01:10-DPB1*12:901', 'HLA-DPA1*01:10-DPB1*13:001', 'HLA-DPA1*01:10-DPB1*13:01', 'HLA-DPA1*01:10-DPB1*13:101',
'HLA-DPA1*01:10-DPB1*13:201', 'HLA-DPA1*01:10-DPB1*13:301', 'HLA-DPA1*01:10-DPB1*13:401', 'HLA-DPA1*01:10-DPB1*14:01', 'HLA-DPA1*01:10-DPB1*15:01',
'HLA-DPA1*01:10-DPB1*16:01', 'HLA-DPA1*01:10-DPB1*17:01', 'HLA-DPA1*01:10-DPB1*18:01', 'HLA-DPA1*01:10-DPB1*19:01', 'HLA-DPA1*01:10-DPB1*20:01',
'HLA-DPA1*01:10-DPB1*21:01', 'HLA-DPA1*01:10-DPB1*22:01', 'HLA-DPA1*01:10-DPB1*23:01', 'HLA-DPA1*01:10-DPB1*24:01', 'HLA-DPA1*01:10-DPB1*25:01',
'HLA-DPA1*01:10-DPB1*26:01', 'HLA-DPA1*01:10-DPB1*27:01', 'HLA-DPA1*01:10-DPB1*28:01', 'HLA-DPA1*01:10-DPB1*29:01', 'HLA-DPA1*01:10-DPB1*30:01',
'HLA-DPA1*01:10-DPB1*31:01', 'HLA-DPA1*01:10-DPB1*32:01', 'HLA-DPA1*01:10-DPB1*33:01', 'HLA-DPA1*01:10-DPB1*34:01', 'HLA-DPA1*01:10-DPB1*35:01',
'HLA-DPA1*01:10-DPB1*36:01', 'HLA-DPA1*01:10-DPB1*37:01', 'HLA-DPA1*01:10-DPB1*38:01', 'HLA-DPA1*01:10-DPB1*39:01', 'HLA-DPA1*01:10-DPB1*40:01',
'HLA-DPA1*01:10-DPB1*41:01', 'HLA-DPA1*01:10-DPB1*44:01', 'HLA-DPA1*01:10-DPB1*45:01', 'HLA-DPA1*01:10-DPB1*46:01', 'HLA-DPA1*01:10-DPB1*47:01',
'HLA-DPA1*01:10-DPB1*48:01', 'HLA-DPA1*01:10-DPB1*49:01', 'HLA-DPA1*01:10-DPB1*50:01', 'HLA-DPA1*01:10-DPB1*51:01', 'HLA-DPA1*01:10-DPB1*52:01',
'HLA-DPA1*01:10-DPB1*53:01', 'HLA-DPA1*01:10-DPB1*54:01', 'HLA-DPA1*01:10-DPB1*55:01', 'HLA-DPA1*01:10-DPB1*56:01', 'HLA-DPA1*01:10-DPB1*58:01',
'HLA-DPA1*01:10-DPB1*59:01', 'HLA-DPA1*01:10-DPB1*60:01', 'HLA-DPA1*01:10-DPB1*62:01', 'HLA-DPA1*01:10-DPB1*63:01', 'HLA-DPA1*01:10-DPB1*65:01',
'HLA-DPA1*01:10-DPB1*66:01', 'HLA-DPA1*01:10-DPB1*67:01', 'HLA-DPA1*01:10-DPB1*68:01', 'HLA-DPA1*01:10-DPB1*69:01', 'HLA-DPA1*01:10-DPB1*70:01',
'HLA-DPA1*01:10-DPB1*71:01', 'HLA-DPA1*01:10-DPB1*72:01', 'HLA-DPA1*01:10-DPB1*73:01', 'HLA-DPA1*01:10-DPB1*74:01', 'HLA-DPA1*01:10-DPB1*75:01',
'HLA-DPA1*01:10-DPB1*76:01', 'HLA-DPA1*01:10-DPB1*77:01', 'HLA-DPA1*01:10-DPB1*78:01', 'HLA-DPA1*01:10-DPB1*79:01', 'HLA-DPA1*01:10-DPB1*80:01',
'HLA-DPA1*01:10-DPB1*81:01', 'HLA-DPA1*01:10-DPB1*82:01', 'HLA-DPA1*01:10-DPB1*83:01', 'HLA-DPA1*01:10-DPB1*84:01', 'HLA-DPA1*01:10-DPB1*85:01',
'HLA-DPA1*01:10-DPB1*86:01', 'HLA-DPA1*01:10-DPB1*87:01', 'HLA-DPA1*01:10-DPB1*88:01', 'HLA-DPA1*01:10-DPB1*89:01', 'HLA-DPA1*01:10-DPB1*90:01',
'HLA-DPA1*01:10-DPB1*91:01', 'HLA-DPA1*01:10-DPB1*92:01', 'HLA-DPA1*01:10-DPB1*93:01', 'HLA-DPA1*01:10-DPB1*94:01', 'HLA-DPA1*01:10-DPB1*95:01',
'HLA-DPA1*01:10-DPB1*96:01', 'HLA-DPA1*01:10-DPB1*97:01', 'HLA-DPA1*01:10-DPB1*98:01', 'HLA-DPA1*01:10-DPB1*99:01', 'HLA-DPA1*02:01-DPB1*01:01',
'HLA-DPA1*02:01-DPB1*02:01', 'HLA-DPA1*02:01-DPB1*02:02', 'HLA-DPA1*02:01-DPB1*03:01', 'HLA-DPA1*02:01-DPB1*04:01', 'HLA-DPA1*02:01-DPB1*04:02',
'HLA-DPA1*02:01-DPB1*05:01', 'HLA-DPA1*02:01-DPB1*06:01', 'HLA-DPA1*02:01-DPB1*08:01', 'HLA-DPA1*02:01-DPB1*09:01', 'HLA-DPA1*02:01-DPB1*10:001',
'HLA-DPA1*02:01-DPB1*10:01', 'HLA-DPA1*02:01-DPB1*10:101', 'HLA-DPA1*02:01-DPB1*10:201', 'HLA-DPA1*02:01-DPB1*10:301', 'HLA-DPA1*02:01-DPB1*10:401',
'HLA-DPA1*02:01-DPB1*10:501', 'HLA-DPA1*02:01-DPB1*10:601', 'HLA-DPA1*02:01-DPB1*10:701', 'HLA-DPA1*02:01-DPB1*10:801', 'HLA-DPA1*02:01-DPB1*10:901',
'HLA-DPA1*02:01-DPB1*11:001', 'HLA-DPA1*02:01-DPB1*11:01', 'HLA-DPA1*02:01-DPB1*11:101', 'HLA-DPA1*02:01-DPB1*11:201', 'HLA-DPA1*02:01-DPB1*11:301',
'HLA-DPA1*02:01-DPB1*11:401', 'HLA-DPA1*02:01-DPB1*11:501', 'HLA-DPA1*02:01-DPB1*11:601', 'HLA-DPA1*02:01-DPB1*11:701', 'HLA-DPA1*02:01-DPB1*11:801',
'HLA-DPA1*02:01-DPB1*11:901', 'HLA-DPA1*02:01-DPB1*12:101', 'HLA-DPA1*02:01-DPB1*12:201', 'HLA-DPA1*02:01-DPB1*12:301', 'HLA-DPA1*02:01-DPB1*12:401',
'HLA-DPA1*02:01-DPB1*12:501', 'HLA-DPA1*02:01-DPB1*12:601', 'HLA-DPA1*02:01-DPB1*12:701', 'HLA-DPA1*02:01-DPB1*12:801', 'HLA-DPA1*02:01-DPB1*12:901',
'HLA-DPA1*02:01-DPB1*13:001', 'HLA-DPA1*02:01-DPB1*13:01', 'HLA-DPA1*02:01-DPB1*13:101', 'HLA-DPA1*02:01-DPB1*13:201', 'HLA-DPA1*02:01-DPB1*13:301',
'HLA-DPA1*02:01-DPB1*13:401', 'HLA-DPA1*02:01-DPB1*14:01', 'HLA-DPA1*02:01-DPB1*15:01', 'HLA-DPA1*02:01-DPB1*16:01', 'HLA-DPA1*02:01-DPB1*17:01',
'HLA-DPA1*02:01-DPB1*18:01', 'HLA-DPA1*02:01-DPB1*19:01', 'HLA-DPA1*02:01-DPB1*20:01', 'HLA-DPA1*02:01-DPB1*21:01', 'HLA-DPA1*02:01-DPB1*22:01',
'HLA-DPA1*02:01-DPB1*23:01', 'HLA-DPA1*02:01-DPB1*24:01', 'HLA-DPA1*02:01-DPB1*25:01', 'HLA-DPA1*02:01-DPB1*26:01', 'HLA-DPA1*02:01-DPB1*27:01',
'HLA-DPA1*02:01-DPB1*28:01', 'HLA-DPA1*02:01-DPB1*29:01', 'HLA-DPA1*02:01-DPB1*30:01', 'HLA-DPA1*02:01-DPB1*31:01', 'HLA-DPA1*02:01-DPB1*32:01',
'HLA-DPA1*02:01-DPB1*33:01', 'HLA-DPA1*02:01-DPB1*34:01', 'HLA-DPA1*02:01-DPB1*35:01', 'HLA-DPA1*02:01-DPB1*36:01', 'HLA-DPA1*02:01-DPB1*37:01',
'HLA-DPA1*02:01-DPB1*38:01', 'HLA-DPA1*02:01-DPB1*39:01', 'HLA-DPA1*02:01-DPB1*40:01', 'HLA-DPA1*02:01-DPB1*41:01', 'HLA-DPA1*02:01-DPB1*44:01',
'HLA-DPA1*02:01-DPB1*45:01', 'HLA-DPA1*02:01-DPB1*46:01', 'HLA-DPA1*02:01-DPB1*47:01', 'HLA-DPA1*02:01-DPB1*48:01', 'HLA-DPA1*02:01-DPB1*49:01',
'HLA-DPA1*02:01-DPB1*50:01', 'HLA-DPA1*02:01-DPB1*51:01', 'HLA-DPA1*02:01-DPB1*52:01', 'HLA-DPA1*02:01-DPB1*53:01', 'HLA-DPA1*02:01-DPB1*54:01',
'HLA-DPA1*02:01-DPB1*55:01', 'HLA-DPA1*02:01-DPB1*56:01', 'HLA-DPA1*02:01-DPB1*58:01', 'HLA-DPA1*02:01-DPB1*59:01', 'HLA-DPA1*02:01-DPB1*60:01',
'HLA-DPA1*02:01-DPB1*62:01', 'HLA-DPA1*02:01-DPB1*63:01', 'HLA-DPA1*02:01-DPB1*65:01', 'HLA-DPA1*02:01-DPB1*66:01', 'HLA-DPA1*02:01-DPB1*67:01',
'HLA-DPA1*02:01-DPB1*68:01', 'HLA-DPA1*02:01-DPB1*69:01', 'HLA-DPA1*02:01-DPB1*70:01', 'HLA-DPA1*02:01-DPB1*71:01', 'HLA-DPA1*02:01-DPB1*72:01',
'HLA-DPA1*02:01-DPB1*73:01', 'HLA-DPA1*02:01-DPB1*74:01', 'HLA-DPA1*02:01-DPB1*75:01', 'HLA-DPA1*02:01-DPB1*76:01', 'HLA-DPA1*02:01-DPB1*77:01',
'HLA-DPA1*02:01-DPB1*78:01', 'HLA-DPA1*02:01-DPB1*79:01', 'HLA-DPA1*02:01-DPB1*80:01', 'HLA-DPA1*02:01-DPB1*81:01', 'HLA-DPA1*02:01-DPB1*82:01',
'HLA-DPA1*02:01-DPB1*83:01', 'HLA-DPA1*02:01-DPB1*84:01', 'HLA-DPA1*02:01-DPB1*85:01', 'HLA-DPA1*02:01-DPB1*86:01', 'HLA-DPA1*02:01-DPB1*87:01',
'HLA-DPA1*02:01-DPB1*88:01', 'HLA-DPA1*02:01-DPB1*89:01', 'HLA-DPA1*02:01-DPB1*90:01', 'HLA-DPA1*02:01-DPB1*91:01', 'HLA-DPA1*02:01-DPB1*92:01',
'HLA-DPA1*02:01-DPB1*93:01', 'HLA-DPA1*02:01-DPB1*94:01', 'HLA-DPA1*02:01-DPB1*95:01', 'HLA-DPA1*02:01-DPB1*96:01', 'HLA-DPA1*02:01-DPB1*97:01',
'HLA-DPA1*02:01-DPB1*98:01', 'HLA-DPA1*02:01-DPB1*99:01', 'HLA-DPA1*02:02-DPB1*01:01', 'HLA-DPA1*02:02-DPB1*02:01', 'HLA-DPA1*02:02-DPB1*02:02',
'HLA-DPA1*02:02-DPB1*03:01', 'HLA-DPA1*02:02-DPB1*04:01', 'HLA-DPA1*02:02-DPB1*04:02', 'HLA-DPA1*02:02-DPB1*05:01', 'HLA-DPA1*02:02-DPB1*06:01',
'HLA-DPA1*02:02-DPB1*08:01', 'HLA-DPA1*02:02-DPB1*09:01', 'HLA-DPA1*02:02-DPB1*10:001', 'HLA-DPA1*02:02-DPB1*10:01', 'HLA-DPA1*02:02-DPB1*10:101',
'HLA-DPA1*02:02-DPB1*10:201', 'HLA-DPA1*02:02-DPB1*10:301', 'HLA-DPA1*02:02-DPB1*10:401', 'HLA-DPA1*02:02-DPB1*10:501', 'HLA-DPA1*02:02-DPB1*10:601',
'HLA-DPA1*02:02-DPB1*10:701', 'HLA-DPA1*02:02-DPB1*10:801', 'HLA-DPA1*02:02-DPB1*10:901', 'HLA-DPA1*02:02-DPB1*11:001', 'HLA-DPA1*02:02-DPB1*11:01',
'HLA-DPA1*02:02-DPB1*11:101', 'HLA-DPA1*02:02-DPB1*11:201', 'HLA-DPA1*02:02-DPB1*11:301', 'HLA-DPA1*02:02-DPB1*11:401', 'HLA-DPA1*02:02-DPB1*11:501',
'HLA-DPA1*02:02-DPB1*11:601', 'HLA-DPA1*02:02-DPB1*11:701', 'HLA-DPA1*02:02-DPB1*11:801', 'HLA-DPA1*02:02-DPB1*11:901', 'HLA-DPA1*02:02-DPB1*12:101',
'HLA-DPA1*02:02-DPB1*12:201', 'HLA-DPA1*02:02-DPB1*12:301', 'HLA-DPA1*02:02-DPB1*12:401', 'HLA-DPA1*02:02-DPB1*12:501', 'HLA-DPA1*02:02-DPB1*12:601',
'HLA-DPA1*02:02-DPB1*12:701', 'HLA-DPA1*02:02-DPB1*12:801', 'HLA-DPA1*02:02-DPB1*12:901', 'HLA-DPA1*02:02-DPB1*13:001', 'HLA-DPA1*02:02-DPB1*13:01',
'HLA-DPA1*02:02-DPB1*13:101', 'HLA-DPA1*02:02-DPB1*13:201', 'HLA-DPA1*02:02-DPB1*13:301', 'HLA-DPA1*02:02-DPB1*13:401', 'HLA-DPA1*02:02-DPB1*14:01',
'HLA-DPA1*02:02-DPB1*15:01', 'HLA-DPA1*02:02-DPB1*16:01', 'HLA-DPA1*02:02-DPB1*17:01', 'HLA-DPA1*02:02-DPB1*18:01', 'HLA-DPA1*02:02-DPB1*19:01',
'HLA-DPA1*02:02-DPB1*20:01', 'HLA-DPA1*02:02-DPB1*21:01', 'HLA-DPA1*02:02-DPB1*22:01', 'HLA-DPA1*02:02-DPB1*23:01', 'HLA-DPA1*02:02-DPB1*24:01',
'HLA-DPA1*02:02-DPB1*25:01', 'HLA-DPA1*02:02-DPB1*26:01', 'HLA-DPA1*02:02-DPB1*27:01', 'HLA-DPA1*02:02-DPB1*28:01', 'HLA-DPA1*02:02-DPB1*29:01',
'HLA-DPA1*02:02-DPB1*30:01', 'HLA-DPA1*02:02-DPB1*31:01', 'HLA-DPA1*02:02-DPB1*32:01', 'HLA-DPA1*02:02-DPB1*33:01', 'HLA-DPA1*02:02-DPB1*34:01',
'HLA-DPA1*02:02-DPB1*35:01', 'HLA-DPA1*02:02-DPB1*36:01', 'HLA-DPA1*02:02-DPB1*37:01', 'HLA-DPA1*02:02-DPB1*38:01', 'HLA-DPA1*02:02-DPB1*39:01',
'HLA-DPA1*02:02-DPB1*40:01', 'HLA-DPA1*02:02-DPB1*41:01', 'HLA-DPA1*02:02-DPB1*44:01', 'HLA-DPA1*02:02-DPB1*45:01', 'HLA-DPA1*02:02-DPB1*46:01',
'HLA-DPA1*02:02-DPB1*47:01', 'HLA-DPA1*02:02-DPB1*48:01', 'HLA-DPA1*02:02-DPB1*49:01', 'HLA-DPA1*02:02-DPB1*50:01', 'HLA-DPA1*02:02-DPB1*51:01',
'HLA-DPA1*02:02-DPB1*52:01', 'HLA-DPA1*02:02-DPB1*53:01', 'HLA-DPA1*02:02-DPB1*54:01', 'HLA-DPA1*02:02-DPB1*55:01', 'HLA-DPA1*02:02-DPB1*56:01',
'HLA-DPA1*02:02-DPB1*58:01', 'HLA-DPA1*02:02-DPB1*59:01', 'HLA-DPA1*02:02-DPB1*60:01', 'HLA-DPA1*02:02-DPB1*62:01', 'HLA-DPA1*02:02-DPB1*63:01',
'HLA-DPA1*02:02-DPB1*65:01', 'HLA-DPA1*02:02-DPB1*66:01', 'HLA-DPA1*02:02-DPB1*67:01', 'HLA-DPA1*02:02-DPB1*68:01', 'HLA-DPA1*02:02-DPB1*69:01',
'HLA-DPA1*02:02-DPB1*70:01', 'HLA-DPA1*02:02-DPB1*71:01', 'HLA-DPA1*02:02-DPB1*72:01', 'HLA-DPA1*02:02-DPB1*73:01', 'HLA-DPA1*02:02-DPB1*74:01',
'HLA-DPA1*02:02-DPB1*75:01', 'HLA-DPA1*02:02-DPB1*76:01', 'HLA-DPA1*02:02-DPB1*77:01', 'HLA-DPA1*02:02-DPB1*78:01', 'HLA-DPA1*02:02-DPB1*79:01',
'HLA-DPA1*02:02-DPB1*80:01', 'HLA-DPA1*02:02-DPB1*81:01', 'HLA-DPA1*02:02-DPB1*82:01', 'HLA-DPA1*02:02-DPB1*83:01', 'HLA-DPA1*02:02-DPB1*84:01',
'HLA-DPA1*02:02-DPB1*85:01', 'HLA-DPA1*02:02-DPB1*86:01', 'HLA-DPA1*02:02-DPB1*87:01', 'HLA-DPA1*02:02-DPB1*88:01', 'HLA-DPA1*02:02-DPB1*89:01',
'HLA-DPA1*02:02-DPB1*90:01', 'HLA-DPA1*02:02-DPB1*91:01', 'HLA-DPA1*02:02-DPB1*92:01', 'HLA-DPA1*02:02-DPB1*93:01', 'HLA-DPA1*02:02-DPB1*94:01',
'HLA-DPA1*02:02-DPB1*95:01', 'HLA-DPA1*02:02-DPB1*96:01', 'HLA-DPA1*02:02-DPB1*97:01', 'HLA-DPA1*02:02-DPB1*98:01', 'HLA-DPA1*02:02-DPB1*99:01',
'HLA-DPA1*02:03-DPB1*01:01', 'HLA-DPA1*02:03-DPB1*02:01', 'HLA-DPA1*02:03-DPB1*02:02', 'HLA-DPA1*02:03-DPB1*03:01', 'HLA-DPA1*02:03-DPB1*04:01',
'HLA-DPA1*02:03-DPB1*04:02', 'HLA-DPA1*02:03-DPB1*05:01', 'HLA-DPA1*02:03-DPB1*06:01', 'HLA-DPA1*02:03-DPB1*08:01', 'HLA-DPA1*02:03-DPB1*09:01',
'HLA-DPA1*02:03-DPB1*10:001', 'HLA-DPA1*02:03-DPB1*10:01', 'HLA-DPA1*02:03-DPB1*10:101', 'HLA-DPA1*02:03-DPB1*10:201', 'HLA-DPA1*02:03-DPB1*10:301',
'HLA-DPA1*02:03-DPB1*10:401', 'HLA-DPA1*02:03-DPB1*10:501', 'HLA-DPA1*02:03-DPB1*10:601', 'HLA-DPA1*02:03-DPB1*10:701', 'HLA-DPA1*02:03-DPB1*10:801',
'HLA-DPA1*02:03-DPB1*10:901', 'HLA-DPA1*02:03-DPB1*11:001', 'HLA-DPA1*02:03-DPB1*11:01', 'HLA-DPA1*02:03-DPB1*11:101', 'HLA-DPA1*02:03-DPB1*11:201',
'HLA-DPA1*02:03-DPB1*11:301', 'HLA-DPA1*02:03-DPB1*11:401', 'HLA-DPA1*02:03-DPB1*11:501', 'HLA-DPA1*02:03-DPB1*11:601', 'HLA-DPA1*02:03-DPB1*11:701',
'HLA-DPA1*02:03-DPB1*11:801', 'HLA-DPA1*02:03-DPB1*11:901', 'HLA-DPA1*02:03-DPB1*12:101', 'HLA-DPA1*02:03-DPB1*12:201', 'HLA-DPA1*02:03-DPB1*12:301',
'HLA-DPA1*02:03-DPB1*12:401', 'HLA-DPA1*02:03-DPB1*12:501', 'HLA-DPA1*02:03-DPB1*12:601', 'HLA-DPA1*02:03-DPB1*12:701', 'HLA-DPA1*02:03-DPB1*12:801',
'HLA-DPA1*02:03-DPB1*12:901', 'HLA-DPA1*02:03-DPB1*13:001', 'HLA-DPA1*02:03-DPB1*13:01', 'HLA-DPA1*02:03-DPB1*13:101', 'HLA-DPA1*02:03-DPB1*13:201',
'HLA-DPA1*02:03-DPB1*13:301', 'HLA-DPA1*02:03-DPB1*13:401', 'HLA-DPA1*02:03-DPB1*14:01', 'HLA-DPA1*02:03-DPB1*15:01', 'HLA-DPA1*02:03-DPB1*16:01',
'HLA-DPA1*02:03-DPB1*17:01', 'HLA-DPA1*02:03-DPB1*18:01', 'HLA-DPA1*02:03-DPB1*19:01', 'HLA-DPA1*02:03-DPB1*20:01', 'HLA-DPA1*02:03-DPB1*21:01',
'HLA-DPA1*02:03-DPB1*22:01', 'HLA-DPA1*02:03-DPB1*23:01', 'HLA-DPA1*02:03-DPB1*24:01', 'HLA-DPA1*02:03-DPB1*25:01', 'HLA-DPA1*02:03-DPB1*26:01',
'HLA-DPA1*02:03-DPB1*27:01', 'HLA-DPA1*02:03-DPB1*28:01', 'HLA-DPA1*02:03-DPB1*29:01', 'HLA-DPA1*02:03-DPB1*30:01', 'HLA-DPA1*02:03-DPB1*31:01',
'HLA-DPA1*02:03-DPB1*32:01', 'HLA-DPA1*02:03-DPB1*33:01', 'HLA-DPA1*02:03-DPB1*34:01', 'HLA-DPA1*02:03-DPB1*35:01', 'HLA-DPA1*02:03-DPB1*36:01',
'HLA-DPA1*02:03-DPB1*37:01', 'HLA-DPA1*02:03-DPB1*38:01', 'HLA-DPA1*02:03-DPB1*39:01', 'HLA-DPA1*02:03-DPB1*40:01', 'HLA-DPA1*02:03-DPB1*41:01',
'HLA-DPA1*02:03-DPB1*44:01', 'HLA-DPA1*02:03-DPB1*45:01', 'HLA-DPA1*02:03-DPB1*46:01', 'HLA-DPA1*02:03-DPB1*47:01', 'HLA-DPA1*02:03-DPB1*48:01',
'HLA-DPA1*02:03-DPB1*49:01', 'HLA-DPA1*02:03-DPB1*50:01', 'HLA-DPA1*02:03-DPB1*51:01', 'HLA-DPA1*02:03-DPB1*52:01', 'HLA-DPA1*02:03-DPB1*53:01',
'HLA-DPA1*02:03-DPB1*54:01', 'HLA-DPA1*02:03-DPB1*55:01', 'HLA-DPA1*02:03-DPB1*56:01', 'HLA-DPA1*02:03-DPB1*58:01', 'HLA-DPA1*02:03-DPB1*59:01',
'HLA-DPA1*02:03-DPB1*60:01', 'HLA-DPA1*02:03-DPB1*62:01', 'HLA-DPA1*02:03-DPB1*63:01', 'HLA-DPA1*02:03-DPB1*65:01', 'HLA-DPA1*02:03-DPB1*66:01',
'HLA-DPA1*02:03-DPB1*67:01', 'HLA-DPA1*02:03-DPB1*68:01', 'HLA-DPA1*02:03-DPB1*69:01', 'HLA-DPA1*02:03-DPB1*70:01', 'HLA-DPA1*02:03-DPB1*71:01',
'HLA-DPA1*02:03-DPB1*72:01', 'HLA-DPA1*02:03-DPB1*73:01', 'HLA-DPA1*02:03-DPB1*74:01', 'HLA-DPA1*02:03-DPB1*75:01', 'HLA-DPA1*02:03-DPB1*76:01',
'HLA-DPA1*02:03-DPB1*77:01', 'HLA-DPA1*02:03-DPB1*78:01', 'HLA-DPA1*02:03-DPB1*79:01', 'HLA-DPA1*02:03-DPB1*80:01', 'HLA-DPA1*02:03-DPB1*81:01',
'HLA-DPA1*02:03-DPB1*82:01', 'HLA-DPA1*02:03-DPB1*83:01', 'HLA-DPA1*02:03-DPB1*84:01', 'HLA-DPA1*02:03-DPB1*85:01', 'HLA-DPA1*02:03-DPB1*86:01',
'HLA-DPA1*02:03-DPB1*87:01', 'HLA-DPA1*02:03-DPB1*88:01', 'HLA-DPA1*02:03-DPB1*89:01', 'HLA-DPA1*02:03-DPB1*90:01', 'HLA-DPA1*02:03-DPB1*91:01',
'HLA-DPA1*02:03-DPB1*92:01', 'HLA-DPA1*02:03-DPB1*93:01', 'HLA-DPA1*02:03-DPB1*94:01', 'HLA-DPA1*02:03-DPB1*95:01', 'HLA-DPA1*02:03-DPB1*96:01',
'HLA-DPA1*02:03-DPB1*97:01', 'HLA-DPA1*02:03-DPB1*98:01', 'HLA-DPA1*02:03-DPB1*99:01', 'HLA-DPA1*02:04-DPB1*01:01', 'HLA-DPA1*02:04-DPB1*02:01',
'HLA-DPA1*02:04-DPB1*02:02', 'HLA-DPA1*02:04-DPB1*03:01', 'HLA-DPA1*02:04-DPB1*04:01', 'HLA-DPA1*02:04-DPB1*04:02', 'HLA-DPA1*02:04-DPB1*05:01',
'HLA-DPA1*02:04-DPB1*06:01', 'HLA-DPA1*02:04-DPB1*08:01', 'HLA-DPA1*02:04-DPB1*09:01', 'HLA-DPA1*02:04-DPB1*10:001', 'HLA-DPA1*02:04-DPB1*10:01',
'HLA-DPA1*02:04-DPB1*10:101', 'HLA-DPA1*02:04-DPB1*10:201', 'HLA-DPA1*02:04-DPB1*10:301', 'HLA-DPA1*02:04-DPB1*10:401', 'HLA-DPA1*02:04-DPB1*10:501',
'HLA-DPA1*02:04-DPB1*10:601', 'HLA-DPA1*02:04-DPB1*10:701', 'HLA-DPA1*02:04-DPB1*10:801', 'HLA-DPA1*02:04-DPB1*10:901', 'HLA-DPA1*02:04-DPB1*11:001',
'HLA-DPA1*02:04-DPB1*11:01', 'HLA-DPA1*02:04-DPB1*11:101', 'HLA-DPA1*02:04-DPB1*11:201', 'HLA-DPA1*02:04-DPB1*11:301', 'HLA-DPA1*02:04-DPB1*11:401',
'HLA-DPA1*02:04-DPB1*11:501', 'HLA-DPA1*02:04-DPB1*11:601', 'HLA-DPA1*02:04-DPB1*11:701', 'HLA-DPA1*02:04-DPB1*11:801', 'HLA-DPA1*02:04-DPB1*11:901',
'HLA-DPA1*02:04-DPB1*12:101', 'HLA-DPA1*02:04-DPB1*12:201', 'HLA-DPA1*02:04-DPB1*12:301', 'HLA-DPA1*02:04-DPB1*12:401', 'HLA-DPA1*02:04-DPB1*12:501',
'HLA-DPA1*02:04-DPB1*12:601', 'HLA-DPA1*02:04-DPB1*12:701', 'HLA-DPA1*02:04-DPB1*12:801', 'HLA-DPA1*02:04-DPB1*12:901', 'HLA-DPA1*02:04-DPB1*13:001',
'HLA-DPA1*02:04-DPB1*13:01', 'HLA-DPA1*02:04-DPB1*13:101', 'HLA-DPA1*02:04-DPB1*13:201', 'HLA-DPA1*02:04-DPB1*13:301', 'HLA-DPA1*02:04-DPB1*13:401',
'HLA-DPA1*02:04-DPB1*14:01', 'HLA-DPA1*02:04-DPB1*15:01', 'HLA-DPA1*02:04-DPB1*16:01', 'HLA-DPA1*02:04-DPB1*17:01', 'HLA-DPA1*02:04-DPB1*18:01',
'HLA-DPA1*02:04-DPB1*19:01', 'HLA-DPA1*02:04-DPB1*20:01', 'HLA-DPA1*02:04-DPB1*21:01', 'HLA-DPA1*02:04-DPB1*22:01', 'HLA-DPA1*02:04-DPB1*23:01',
'HLA-DPA1*02:04-DPB1*24:01', 'HLA-DPA1*02:04-DPB1*25:01', 'HLA-DPA1*02:04-DPB1*26:01', 'HLA-DPA1*02:04-DPB1*27:01', 'HLA-DPA1*02:04-DPB1*28:01',
'HLA-DPA1*02:04-DPB1*29:01', 'HLA-DPA1*02:04-DPB1*30:01', 'HLA-DPA1*02:04-DPB1*31:01', 'HLA-DPA1*02:04-DPB1*32:01', 'HLA-DPA1*02:04-DPB1*33:01',
'HLA-DPA1*02:04-DPB1*34:01', 'HLA-DPA1*02:04-DPB1*35:01', 'HLA-DPA1*02:04-DPB1*36:01', 'HLA-DPA1*02:04-DPB1*37:01', 'HLA-DPA1*02:04-DPB1*38:01',
'HLA-DPA1*02:04-DPB1*39:01', 'HLA-DPA1*02:04-DPB1*40:01', 'HLA-DPA1*02:04-DPB1*41:01', 'HLA-DPA1*02:04-DPB1*44:01', 'HLA-DPA1*02:04-DPB1*45:01',
'HLA-DPA1*02:04-DPB1*46:01', 'HLA-DPA1*02:04-DPB1*47:01', 'HLA-DPA1*02:04-DPB1*48:01', 'HLA-DPA1*02:04-DPB1*49:01', 'HLA-DPA1*02:04-DPB1*50:01',
'HLA-DPA1*02:04-DPB1*51:01', 'HLA-DPA1*02:04-DPB1*52:01', 'HLA-DPA1*02:04-DPB1*53:01', 'HLA-DPA1*02:04-DPB1*54:01', 'HLA-DPA1*02:04-DPB1*55:01',
'HLA-DPA1*02:04-DPB1*56:01', 'HLA-DPA1*02:04-DPB1*58:01', 'HLA-DPA1*02:04-DPB1*59:01', 'HLA-DPA1*02:04-DPB1*60:01', 'HLA-DPA1*02:04-DPB1*62:01',
'HLA-DPA1*02:04-DPB1*63:01', 'HLA-DPA1*02:04-DPB1*65:01', 'HLA-DPA1*02:04-DPB1*66:01', 'HLA-DPA1*02:04-DPB1*67:01', 'HLA-DPA1*02:04-DPB1*68:01',
'HLA-DPA1*02:04-DPB1*69:01', 'HLA-DPA1*02:04-DPB1*70:01', 'HLA-DPA1*02:04-DPB1*71:01', 'HLA-DPA1*02:04-DPB1*72:01', 'HLA-DPA1*02:04-DPB1*73:01',
'HLA-DPA1*02:04-DPB1*74:01', 'HLA-DPA1*02:04-DPB1*75:01', 'HLA-DPA1*02:04-DPB1*76:01', 'HLA-DPA1*02:04-DPB1*77:01', 'HLA-DPA1*02:04-DPB1*78:01',
'HLA-DPA1*02:04-DPB1*79:01', 'HLA-DPA1*02:04-DPB1*80:01', 'HLA-DPA1*02:04-DPB1*81:01', 'HLA-DPA1*02:04-DPB1*82:01', 'HLA-DPA1*02:04-DPB1*83:01',
'HLA-DPA1*02:04-DPB1*84:01', 'HLA-DPA1*02:04-DPB1*85:01', 'HLA-DPA1*02:04-DPB1*86:01', 'HLA-DPA1*02:04-DPB1*87:01', 'HLA-DPA1*02:04-DPB1*88:01',
'HLA-DPA1*02:04-DPB1*89:01', 'HLA-DPA1*02:04-DPB1*90:01', 'HLA-DPA1*02:04-DPB1*91:01', 'HLA-DPA1*02:04-DPB1*92:01', 'HLA-DPA1*02:04-DPB1*93:01',
'HLA-DPA1*02:04-DPB1*94:01', 'HLA-DPA1*02:04-DPB1*95:01', 'HLA-DPA1*02:04-DPB1*96:01', 'HLA-DPA1*02:04-DPB1*97:01', 'HLA-DPA1*02:04-DPB1*98:01',
'HLA-DPA1*02:04-DPB1*99:01', 'HLA-DPA1*03:01-DPB1*01:01', 'HLA-DPA1*03:01-DPB1*02:01', 'HLA-DPA1*03:01-DPB1*02:02', 'HLA-DPA1*03:01-DPB1*03:01',
'HLA-DPA1*03:01-DPB1*04:01', 'HLA-DPA1*03:01-DPB1*04:02', 'HLA-DPA1*03:01-DPB1*05:01', 'HLA-DPA1*03:01-DPB1*06:01', 'HLA-DPA1*03:01-DPB1*08:01',
'HLA-DPA1*03:01-DPB1*09:01', 'HLA-DPA1*03:01-DPB1*10:001', 'HLA-DPA1*03:01-DPB1*10:01', 'HLA-DPA1*03:01-DPB1*10:101', 'HLA-DPA1*03:01-DPB1*10:201',
'HLA-DPA1*03:01-DPB1*10:301', 'HLA-DPA1*03:01-DPB1*10:401', 'HLA-DPA1*03:01-DPB1*10:501', 'HLA-DPA1*03:01-DPB1*10:601', 'HLA-DPA1*03:01-DPB1*10:701',
'HLA-DPA1*03:01-DPB1*10:801', 'HLA-DPA1*03:01-DPB1*10:901', 'HLA-DPA1*03:01-DPB1*11:001', 'HLA-DPA1*03:01-DPB1*11:01', 'HLA-DPA1*03:01-DPB1*11:101',
'HLA-DPA1*03:01-DPB1*11:201', 'HLA-DPA1*03:01-DPB1*11:301', 'HLA-DPA1*03:01-DPB1*11:401', 'HLA-DPA1*03:01-DPB1*11:501', 'HLA-DPA1*03:01-DPB1*11:601',
'HLA-DPA1*03:01-DPB1*11:701', 'HLA-DPA1*03:01-DPB1*11:801', 'HLA-DPA1*03:01-DPB1*11:901', 'HLA-DPA1*03:01-DPB1*12:101', 'HLA-DPA1*03:01-DPB1*12:201',
'HLA-DPA1*03:01-DPB1*12:301', 'HLA-DPA1*03:01-DPB1*12:401', 'HLA-DPA1*03:01-DPB1*12:501', 'HLA-DPA1*03:01-DPB1*12:601', 'HLA-DPA1*03:01-DPB1*12:701',
'HLA-DPA1*03:01-DPB1*12:801', 'HLA-DPA1*03:01-DPB1*12:901', 'HLA-DPA1*03:01-DPB1*13:001', 'HLA-DPA1*03:01-DPB1*13:01', 'HLA-DPA1*03:01-DPB1*13:101',
'HLA-DPA1*03:01-DPB1*13:201', 'HLA-DPA1*03:01-DPB1*13:301', 'HLA-DPA1*03:01-DPB1*13:401', 'HLA-DPA1*03:01-DPB1*14:01', 'HLA-DPA1*03:01-DPB1*15:01',
'HLA-DPA1*03:01-DPB1*16:01', 'HLA-DPA1*03:01-DPB1*17:01', 'HLA-DPA1*03:01-DPB1*18:01', 'HLA-DPA1*03:01-DPB1*19:01', 'HLA-DPA1*03:01-DPB1*20:01',
'HLA-DPA1*03:01-DPB1*21:01', 'HLA-DPA1*03:01-DPB1*22:01', 'HLA-DPA1*03:01-DPB1*23:01', 'HLA-DPA1*03:01-DPB1*24:01', 'HLA-DPA1*03:01-DPB1*25:01',
'HLA-DPA1*03:01-DPB1*26:01', 'HLA-DPA1*03:01-DPB1*27:01', 'HLA-DPA1*03:01-DPB1*28:01', 'HLA-DPA1*03:01-DPB1*29:01', 'HLA-DPA1*03:01-DPB1*30:01',
'HLA-DPA1*03:01-DPB1*31:01', 'HLA-DPA1*03:01-DPB1*32:01', 'HLA-DPA1*03:01-DPB1*33:01', 'HLA-DPA1*03:01-DPB1*34:01', 'HLA-DPA1*03:01-DPB1*35:01',
'HLA-DPA1*03:01-DPB1*36:01', 'HLA-DPA1*03:01-DPB1*37:01', 'HLA-DPA1*03:01-DPB1*38:01', 'HLA-DPA1*03:01-DPB1*39:01', 'HLA-DPA1*03:01-DPB1*40:01',
'HLA-DPA1*03:01-DPB1*41:01', 'HLA-DPA1*03:01-DPB1*44:01', 'HLA-DPA1*03:01-DPB1*45:01', 'HLA-DPA1*03:01-DPB1*46:01', 'HLA-DPA1*03:01-DPB1*47:01',
'HLA-DPA1*03:01-DPB1*48:01', 'HLA-DPA1*03:01-DPB1*49:01', 'HLA-DPA1*03:01-DPB1*50:01', 'HLA-DPA1*03:01-DPB1*51:01', 'HLA-DPA1*03:01-DPB1*52:01',
'HLA-DPA1*03:01-DPB1*53:01', 'HLA-DPA1*03:01-DPB1*54:01', 'HLA-DPA1*03:01-DPB1*55:01', 'HLA-DPA1*03:01-DPB1*56:01', 'HLA-DPA1*03:01-DPB1*58:01',
'HLA-DPA1*03:01-DPB1*59:01', 'HLA-DPA1*03:01-DPB1*60:01', 'HLA-DPA1*03:01-DPB1*62:01', 'HLA-DPA1*03:01-DPB1*63:01', 'HLA-DPA1*03:01-DPB1*65:01',
'HLA-DPA1*03:01-DPB1*66:01', 'HLA-DPA1*03:01-DPB1*67:01', 'HLA-DPA1*03:01-DPB1*68:01', 'HLA-DPA1*03:01-DPB1*69:01', 'HLA-DPA1*03:01-DPB1*70:01',
'HLA-DPA1*03:01-DPB1*71:01', 'HLA-DPA1*03:01-DPB1*72:01', 'HLA-DPA1*03:01-DPB1*73:01', 'HLA-DPA1*03:01-DPB1*74:01', 'HLA-DPA1*03:01-DPB1*75:01',
'HLA-DPA1*03:01-DPB1*76:01', 'HLA-DPA1*03:01-DPB1*77:01', 'HLA-DPA1*03:01-DPB1*78:01', 'HLA-DPA1*03:01-DPB1*79:01', 'HLA-DPA1*03:01-DPB1*80:01',
'HLA-DPA1*03:01-DPB1*81:01', 'HLA-DPA1*03:01-DPB1*82:01', 'HLA-DPA1*03:01-DPB1*83:01', 'HLA-DPA1*03:01-DPB1*84:01', 'HLA-DPA1*03:01-DPB1*85:01',
'HLA-DPA1*03:01-DPB1*86:01', 'HLA-DPA1*03:01-DPB1*87:01', 'HLA-DPA1*03:01-DPB1*88:01', 'HLA-DPA1*03:01-DPB1*89:01', 'HLA-DPA1*03:01-DPB1*90:01',
'HLA-DPA1*03:01-DPB1*91:01', 'HLA-DPA1*03:01-DPB1*92:01', 'HLA-DPA1*03:01-DPB1*93:01', 'HLA-DPA1*03:01-DPB1*94:01', 'HLA-DPA1*03:01-DPB1*95:01',
'HLA-DPA1*03:01-DPB1*96:01', 'HLA-DPA1*03:01-DPB1*97:01', 'HLA-DPA1*03:01-DPB1*98:01', 'HLA-DPA1*03:01-DPB1*99:01', 'HLA-DPA1*03:02-DPB1*01:01',
'HLA-DPA1*03:02-DPB1*02:01', 'HLA-DPA1*03:02-DPB1*02:02', 'HLA-DPA1*03:02-DPB1*03:01', 'HLA-DPA1*03:02-DPB1*04:01', 'HLA-DPA1*03:02-DPB1*04:02',
'HLA-DPA1*03:02-DPB1*05:01', 'HLA-DPA1*03:02-DPB1*06:01', 'HLA-DPA1*03:02-DPB1*08:01', 'HLA-DPA1*03:02-DPB1*09:01', 'HLA-DPA1*03:02-DPB1*10:001',
'HLA-DPA1*03:02-DPB1*10:01', 'HLA-DPA1*03:02-DPB1*10:101', 'HLA-DPA1*03:02-DPB1*10:201', 'HLA-DPA1*03:02-DPB1*10:301', 'HLA-DPA1*03:02-DPB1*10:401',
'HLA-DPA1*03:02-DPB1*10:501', 'HLA-DPA1*03:02-DPB1*10:601', 'HLA-DPA1*03:02-DPB1*10:701', 'HLA-DPA1*03:02-DPB1*10:801', 'HLA-DPA1*03:02-DPB1*10:901',
'HLA-DPA1*03:02-DPB1*11:001', 'HLA-DPA1*03:02-DPB1*11:01', 'HLA-DPA1*03:02-DPB1*11:101', 'HLA-DPA1*03:02-DPB1*11:201', 'HLA-DPA1*03:02-DPB1*11:301',
'HLA-DPA1*03:02-DPB1*11:401', 'HLA-DPA1*03:02-DPB1*11:501', 'HLA-DPA1*03:02-DPB1*11:601', 'HLA-DPA1*03:02-DPB1*11:701', 'HLA-DPA1*03:02-DPB1*11:801',
'HLA-DPA1*03:02-DPB1*11:901', 'HLA-DPA1*03:02-DPB1*12:101', 'HLA-DPA1*03:02-DPB1*12:201', 'HLA-DPA1*03:02-DPB1*12:301', 'HLA-DPA1*03:02-DPB1*12:401',
'HLA-DPA1*03:02-DPB1*12:501', 'HLA-DPA1*03:02-DPB1*12:601', 'HLA-DPA1*03:02-DPB1*12:701', 'HLA-DPA1*03:02-DPB1*12:801', 'HLA-DPA1*03:02-DPB1*12:901',
'HLA-DPA1*03:02-DPB1*13:001', 'HLA-DPA1*03:02-DPB1*13:01', 'HLA-DPA1*03:02-DPB1*13:101', 'HLA-DPA1*03:02-DPB1*13:201', 'HLA-DPA1*03:02-DPB1*13:301',
'HLA-DPA1*03:02-DPB1*13:401', 'HLA-DPA1*03:02-DPB1*14:01', 'HLA-DPA1*03:02-DPB1*15:01', 'HLA-DPA1*03:02-DPB1*16:01', 'HLA-DPA1*03:02-DPB1*17:01',
'HLA-DPA1*03:02-DPB1*18:01', 'HLA-DPA1*03:02-DPB1*19:01', 'HLA-DPA1*03:02-DPB1*20:01', 'HLA-DPA1*03:02-DPB1*21:01', 'HLA-DPA1*03:02-DPB1*22:01',
'HLA-DPA1*03:02-DPB1*23:01', 'HLA-DPA1*03:02-DPB1*24:01', 'HLA-DPA1*03:02-DPB1*25:01', 'HLA-DPA1*03:02-DPB1*26:01', 'HLA-DPA1*03:02-DPB1*27:01',
'HLA-DPA1*03:02-DPB1*28:01', 'HLA-DPA1*03:02-DPB1*29:01', 'HLA-DPA1*03:02-DPB1*30:01', 'HLA-DPA1*03:02-DPB1*31:01', 'HLA-DPA1*03:02-DPB1*32:01',
'HLA-DPA1*03:02-DPB1*33:01', 'HLA-DPA1*03:02-DPB1*34:01', 'HLA-DPA1*03:02-DPB1*35:01', 'HLA-DPA1*03:02-DPB1*36:01', 'HLA-DPA1*03:02-DPB1*37:01',
'HLA-DPA1*03:02-DPB1*38:01', 'HLA-DPA1*03:02-DPB1*39:01', 'HLA-DPA1*03:02-DPB1*40:01', 'HLA-DPA1*03:02-DPB1*41:01', 'HLA-DPA1*03:02-DPB1*44:01',
'HLA-DPA1*03:02-DPB1*45:01', 'HLA-DPA1*03:02-DPB1*46:01', 'HLA-DPA1*03:02-DPB1*47:01', 'HLA-DPA1*03:02-DPB1*48:01', 'HLA-DPA1*03:02-DPB1*49:01',
'HLA-DPA1*03:02-DPB1*50:01', 'HLA-DPA1*03:02-DPB1*51:01', 'HLA-DPA1*03:02-DPB1*52:01', 'HLA-DPA1*03:02-DPB1*53:01', 'HLA-DPA1*03:02-DPB1*54:01',
'HLA-DPA1*03:02-DPB1*55:01', 'HLA-DPA1*03:02-DPB1*56:01', 'HLA-DPA1*03:02-DPB1*58:01', 'HLA-DPA1*03:02-DPB1*59:01', 'HLA-DPA1*03:02-DPB1*60:01',
'HLA-DPA1*03:02-DPB1*62:01', 'HLA-DPA1*03:02-DPB1*63:01', 'HLA-DPA1*03:02-DPB1*65:01', 'HLA-DPA1*03:02-DPB1*66:01', 'HLA-DPA1*03:02-DPB1*67:01',
'HLA-DPA1*03:02-DPB1*68:01', 'HLA-DPA1*03:02-DPB1*69:01', 'HLA-DPA1*03:02-DPB1*70:01', 'HLA-DPA1*03:02-DPB1*71:01', 'HLA-DPA1*03:02-DPB1*72:01',
'HLA-DPA1*03:02-DPB1*73:01', 'HLA-DPA1*03:02-DPB1*74:01', 'HLA-DPA1*03:02-DPB1*75:01', 'HLA-DPA1*03:02-DPB1*76:01', 'HLA-DPA1*03:02-DPB1*77:01',
'HLA-DPA1*03:02-DPB1*78:01', 'HLA-DPA1*03:02-DPB1*79:01', 'HLA-DPA1*03:02-DPB1*80:01', 'HLA-DPA1*03:02-DPB1*81:01', 'HLA-DPA1*03:02-DPB1*82:01',
'HLA-DPA1*03:02-DPB1*83:01', 'HLA-DPA1*03:02-DPB1*84:01', 'HLA-DPA1*03:02-DPB1*85:01', 'HLA-DPA1*03:02-DPB1*86:01', 'HLA-DPA1*03:02-DPB1*87:01',
'HLA-DPA1*03:02-DPB1*88:01', 'HLA-DPA1*03:02-DPB1*89:01', 'HLA-DPA1*03:02-DPB1*90:01', 'HLA-DPA1*03:02-DPB1*91:01', 'HLA-DPA1*03:02-DPB1*92:01',
'HLA-DPA1*03:02-DPB1*93:01', 'HLA-DPA1*03:02-DPB1*94:01', 'HLA-DPA1*03:02-DPB1*95:01', 'HLA-DPA1*03:02-DPB1*96:01', 'HLA-DPA1*03:02-DPB1*97:01',
'HLA-DPA1*03:02-DPB1*98:01', 'HLA-DPA1*03:02-DPB1*99:01', 'HLA-DPA1*03:03-DPB1*01:01', 'HLA-DPA1*03:03-DPB1*02:01', 'HLA-DPA1*03:03-DPB1*02:02',
'HLA-DPA1*03:03-DPB1*03:01', 'HLA-DPA1*03:03-DPB1*04:01', 'HLA-DPA1*03:03-DPB1*04:02', 'HLA-DPA1*03:03-DPB1*05:01', 'HLA-DPA1*03:03-DPB1*06:01',
'HLA-DPA1*03:03-DPB1*08:01', 'HLA-DPA1*03:03-DPB1*09:01', 'HLA-DPA1*03:03-DPB1*10:001', 'HLA-DPA1*03:03-DPB1*10:01', 'HLA-DPA1*03:03-DPB1*10:101',
'HLA-DPA1*03:03-DPB1*10:201', 'HLA-DPA1*03:03-DPB1*10:301', 'HLA-DPA1*03:03-DPB1*10:401', 'HLA-DPA1*03:03-DPB1*10:501', 'HLA-DPA1*03:03-DPB1*10:601',
'HLA-DPA1*03:03-DPB1*10:701', 'HLA-DPA1*03:03-DPB1*10:801', 'HLA-DPA1*03:03-DPB1*10:901', 'HLA-DPA1*03:03-DPB1*11:001', 'HLA-DPA1*03:03-DPB1*11:01',
'HLA-DPA1*03:03-DPB1*11:101', 'HLA-DPA1*03:03-DPB1*11:201', 'HLA-DPA1*03:03-DPB1*11:301', 'HLA-DPA1*03:03-DPB1*11:401', 'HLA-DPA1*03:03-DPB1*11:501',
'HLA-DPA1*03:03-DPB1*11:601', 'HLA-DPA1*03:03-DPB1*11:701', 'HLA-DPA1*03:03-DPB1*11:801', 'HLA-DPA1*03:03-DPB1*11:901', 'HLA-DPA1*03:03-DPB1*12:101',
'HLA-DPA1*03:03-DPB1*12:201', 'HLA-DPA1*03:03-DPB1*12:301', 'HLA-DPA1*03:03-DPB1*12:401', 'HLA-DPA1*03:03-DPB1*12:501', 'HLA-DPA1*03:03-DPB1*12:601',
'HLA-DPA1*03:03-DPB1*12:701', 'HLA-DPA1*03:03-DPB1*12:801', 'HLA-DPA1*03:03-DPB1*12:901', 'HLA-DPA1*03:03-DPB1*13:001', 'HLA-DPA1*03:03-DPB1*13:01',
'HLA-DPA1*03:03-DPB1*13:101', 'HLA-DPA1*03:03-DPB1*13:201', 'HLA-DPA1*03:03-DPB1*13:301', 'HLA-DPA1*03:03-DPB1*13:401', 'HLA-DPA1*03:03-DPB1*14:01',
'HLA-DPA1*03:03-DPB1*15:01', 'HLA-DPA1*03:03-DPB1*16:01', 'HLA-DPA1*03:03-DPB1*17:01', 'HLA-DPA1*03:03-DPB1*18:01', 'HLA-DPA1*03:03-DPB1*19:01',
'HLA-DPA1*03:03-DPB1*20:01', 'HLA-DPA1*03:03-DPB1*21:01', 'HLA-DPA1*03:03-DPB1*22:01', 'HLA-DPA1*03:03-DPB1*23:01', 'HLA-DPA1*03:03-DPB1*24:01',
'HLA-DPA1*03:03-DPB1*25:01', 'HLA-DPA1*03:03-DPB1*26:01', 'HLA-DPA1*03:03-DPB1*27:01', 'HLA-DPA1*03:03-DPB1*28:01', 'HLA-DPA1*03:03-DPB1*29:01',
'HLA-DPA1*03:03-DPB1*30:01', 'HLA-DPA1*03:03-DPB1*31:01', 'HLA-DPA1*03:03-DPB1*32:01', 'HLA-DPA1*03:03-DPB1*33:01', 'HLA-DPA1*03:03-DPB1*34:01',
'HLA-DPA1*03:03-DPB1*35:01', 'HLA-DPA1*03:03-DPB1*36:01', 'HLA-DPA1*03:03-DPB1*37:01', 'HLA-DPA1*03:03-DPB1*38:01', 'HLA-DPA1*03:03-DPB1*39:01',
'HLA-DPA1*03:03-DPB1*40:01', 'HLA-DPA1*03:03-DPB1*41:01', 'HLA-DPA1*03:03-DPB1*44:01', 'HLA-DPA1*03:03-DPB1*45:01', 'HLA-DPA1*03:03-DPB1*46:01',
'HLA-DPA1*03:03-DPB1*47:01', 'HLA-DPA1*03:03-DPB1*48:01', 'HLA-DPA1*03:03-DPB1*49:01', 'HLA-DPA1*03:03-DPB1*50:01', 'HLA-DPA1*03:03-DPB1*51:01',
'HLA-DPA1*03:03-DPB1*52:01', 'HLA-DPA1*03:03-DPB1*53:01', 'HLA-DPA1*03:03-DPB1*54:01', 'HLA-DPA1*03:03-DPB1*55:01', 'HLA-DPA1*03:03-DPB1*56:01',
'HLA-DPA1*03:03-DPB1*58:01', 'HLA-DPA1*03:03-DPB1*59:01', 'HLA-DPA1*03:03-DPB1*60:01', 'HLA-DPA1*03:03-DPB1*62:01', 'HLA-DPA1*03:03-DPB1*63:01',
'HLA-DPA1*03:03-DPB1*65:01', 'HLA-DPA1*03:03-DPB1*66:01', 'HLA-DPA1*03:03-DPB1*67:01', 'HLA-DPA1*03:03-DPB1*68:01', 'HLA-DPA1*03:03-DPB1*69:01',
'HLA-DPA1*03:03-DPB1*70:01', 'HLA-DPA1*03:03-DPB1*71:01', 'HLA-DPA1*03:03-DPB1*72:01', 'HLA-DPA1*03:03-DPB1*73:01', 'HLA-DPA1*03:03-DPB1*74:01',
'HLA-DPA1*03:03-DPB1*75:01', 'HLA-DPA1*03:03-DPB1*76:01', 'HLA-DPA1*03:03-DPB1*77:01', 'HLA-DPA1*03:03-DPB1*78:01', 'HLA-DPA1*03:03-DPB1*79:01',
'HLA-DPA1*03:03-DPB1*80:01', 'HLA-DPA1*03:03-DPB1*81:01', 'HLA-DPA1*03:03-DPB1*82:01', 'HLA-DPA1*03:03-DPB1*83:01', 'HLA-DPA1*03:03-DPB1*84:01',
'HLA-DPA1*03:03-DPB1*85:01', 'HLA-DPA1*03:03-DPB1*86:01', 'HLA-DPA1*03:03-DPB1*87:01', 'HLA-DPA1*03:03-DPB1*88:01', 'HLA-DPA1*03:03-DPB1*89:01',
'HLA-DPA1*03:03-DPB1*90:01', 'HLA-DPA1*03:03-DPB1*91:01', 'HLA-DPA1*03:03-DPB1*92:01', 'HLA-DPA1*03:03-DPB1*93:01', 'HLA-DPA1*03:03-DPB1*94:01',
'HLA-DPA1*03:03-DPB1*95:01', 'HLA-DPA1*03:03-DPB1*96:01', 'HLA-DPA1*03:03-DPB1*97:01', 'HLA-DPA1*03:03-DPB1*98:01', 'HLA-DPA1*03:03-DPB1*99:01',
'HLA-DPA1*04:01-DPB1*01:01', 'HLA-DPA1*04:01-DPB1*02:01', 'HLA-DPA1*04:01-DPB1*02:02', 'HLA-DPA1*04:01-DPB1*03:01', 'HLA-DPA1*04:01-DPB1*04:01',
'HLA-DPA1*04:01-DPB1*04:02', 'HLA-DPA1*04:01-DPB1*05:01', 'HLA-DPA1*04:01-DPB1*06:01', 'HLA-DPA1*04:01-DPB1*08:01', 'HLA-DPA1*04:01-DPB1*09:01',
'HLA-DPA1*04:01-DPB1*10:001', 'HLA-DPA1*04:01-DPB1*10:01', 'HLA-DPA1*04:01-DPB1*10:101', 'HLA-DPA1*04:01-DPB1*10:201', 'HLA-DPA1*04:01-DPB1*10:301',
'HLA-DPA1*04:01-DPB1*10:401', 'HLA-DPA1*04:01-DPB1*10:501', 'HLA-DPA1*04:01-DPB1*10:601', 'HLA-DPA1*04:01-DPB1*10:701', 'HLA-DPA1*04:01-DPB1*10:801',
'HLA-DPA1*04:01-DPB1*10:901', 'HLA-DPA1*04:01-DPB1*11:001', 'HLA-DPA1*04:01-DPB1*11:01', 'HLA-DPA1*04:01-DPB1*11:101', 'HLA-DPA1*04:01-DPB1*11:201',
'HLA-DPA1*04:01-DPB1*11:301', 'HLA-DPA1*04:01-DPB1*11:401', 'HLA-DPA1*04:01-DPB1*11:501', 'HLA-DPA1*04:01-DPB1*11:601', 'HLA-DPA1*04:01-DPB1*11:701',
'HLA-DPA1*04:01-DPB1*11:801', 'HLA-DPA1*04:01-DPB1*11:901', 'HLA-DPA1*04:01-DPB1*12:101', 'HLA-DPA1*04:01-DPB1*12:201', 'HLA-DPA1*04:01-DPB1*12:301',
'HLA-DPA1*04:01-DPB1*12:401', 'HLA-DPA1*04:01-DPB1*12:501', 'HLA-DPA1*04:01-DPB1*12:601', 'HLA-DPA1*04:01-DPB1*12:701', 'HLA-DPA1*04:01-DPB1*12:801',
'HLA-DPA1*04:01-DPB1*12:901', 'HLA-DPA1*04:01-DPB1*13:001', 'HLA-DPA1*04:01-DPB1*13:01', 'HLA-DPA1*04:01-DPB1*13:101', 'HLA-DPA1*04:01-DPB1*13:201',
'HLA-DPA1*04:01-DPB1*13:301', 'HLA-DPA1*04:01-DPB1*13:401', 'HLA-DPA1*04:01-DPB1*14:01', 'HLA-DPA1*04:01-DPB1*15:01', 'HLA-DPA1*04:01-DPB1*16:01',
'HLA-DPA1*04:01-DPB1*17:01', 'HLA-DPA1*04:01-DPB1*18:01', 'HLA-DPA1*04:01-DPB1*19:01', 'HLA-DPA1*04:01-DPB1*20:01', 'HLA-DPA1*04:01-DPB1*21:01',
'HLA-DPA1*04:01-DPB1*22:01', 'HLA-DPA1*04:01-DPB1*23:01', 'HLA-DPA1*04:01-DPB1*24:01', 'HLA-DPA1*04:01-DPB1*25:01', 'HLA-DPA1*04:01-DPB1*26:01',
'HLA-DPA1*04:01-DPB1*27:01', 'HLA-DPA1*04:01-DPB1*28:01', 'HLA-DPA1*04:01-DPB1*29:01', 'HLA-DPA1*04:01-DPB1*30:01', 'HLA-DPA1*04:01-DPB1*31:01',
'HLA-DPA1*04:01-DPB1*32:01', 'HLA-DPA1*04:01-DPB1*33:01', 'HLA-DPA1*04:01-DPB1*34:01', 'HLA-DPA1*04:01-DPB1*35:01', 'HLA-DPA1*04:01-DPB1*36:01',
'HLA-DPA1*04:01-DPB1*37:01', 'HLA-DPA1*04:01-DPB1*38:01', 'HLA-DPA1*04:01-DPB1*39:01', 'HLA-DPA1*04:01-DPB1*40:01', 'HLA-DPA1*04:01-DPB1*41:01',
'HLA-DPA1*04:01-DPB1*44:01', 'HLA-DPA1*04:01-DPB1*45:01', 'HLA-DPA1*04:01-DPB1*46:01', 'HLA-DPA1*04:01-DPB1*47:01', 'HLA-DPA1*04:01-DPB1*48:01',
'HLA-DPA1*04:01-DPB1*49:01', 'HLA-DPA1*04:01-DPB1*50:01', 'HLA-DPA1*04:01-DPB1*51:01', 'HLA-DPA1*04:01-DPB1*52:01', 'HLA-DPA1*04:01-DPB1*53:01',
'HLA-DPA1*04:01-DPB1*54:01', 'HLA-DPA1*04:01-DPB1*55:01', 'HLA-DPA1*04:01-DPB1*56:01', 'HLA-DPA1*04:01-DPB1*58:01', 'HLA-DPA1*04:01-DPB1*59:01',
'HLA-DPA1*04:01-DPB1*60:01', 'HLA-DPA1*04:01-DPB1*62:01', 'HLA-DPA1*04:01-DPB1*63:01', 'HLA-DPA1*04:01-DPB1*65:01', 'HLA-DPA1*04:01-DPB1*66:01',
'HLA-DPA1*04:01-DPB1*67:01', 'HLA-DPA1*04:01-DPB1*68:01', 'HLA-DPA1*04:01-DPB1*69:01', 'HLA-DPA1*04:01-DPB1*70:01', 'HLA-DPA1*04:01-DPB1*71:01',
'HLA-DPA1*04:01-DPB1*72:01', 'HLA-DPA1*04:01-DPB1*73:01', 'HLA-DPA1*04:01-DPB1*74:01', 'HLA-DPA1*04:01-DPB1*75:01', 'HLA-DPA1*04:01-DPB1*76:01',
'HLA-DPA1*04:01-DPB1*77:01', 'HLA-DPA1*04:01-DPB1*78:01', 'HLA-DPA1*04:01-DPB1*79:01', 'HLA-DPA1*04:01-DPB1*80:01', 'HLA-DPA1*04:01-DPB1*81:01',
'HLA-DPA1*04:01-DPB1*82:01', 'HLA-DPA1*04:01-DPB1*83:01', 'HLA-DPA1*04:01-DPB1*84:01', 'HLA-DPA1*04:01-DPB1*85:01', 'HLA-DPA1*04:01-DPB1*86:01',
'HLA-DPA1*04:01-DPB1*87:01', 'HLA-DPA1*04:01-DPB1*88:01', 'HLA-DPA1*04:01-DPB1*89:01', 'HLA-DPA1*04:01-DPB1*90:01', 'HLA-DPA1*04:01-DPB1*91:01',
'HLA-DPA1*04:01-DPB1*92:01', 'HLA-DPA1*04:01-DPB1*93:01', 'HLA-DPA1*04:01-DPB1*94:01', 'HLA-DPA1*04:01-DPB1*95:01', 'HLA-DPA1*04:01-DPB1*96:01',
'HLA-DPA1*04:01-DPB1*97:01', 'HLA-DPA1*04:01-DPB1*98:01', 'HLA-DPA1*04:01-DPB1*99:01', 'HLA-DQA1*01:01-DQB1*02:01', 'HLA-DQA1*01:01-DQB1*02:02',
'HLA-DQA1*01:01-DQB1*02:03', 'HLA-DQA1*01:01-DQB1*02:04', 'HLA-DQA1*01:01-DQB1*02:05', 'HLA-DQA1*01:01-DQB1*02:06', 'HLA-DQA1*01:01-DQB1*03:01',
'HLA-DQA1*01:01-DQB1*03:02', 'HLA-DQA1*01:01-DQB1*03:03', 'HLA-DQA1*01:01-DQB1*03:04', 'HLA-DQA1*01:01-DQB1*03:05', 'HLA-DQA1*01:01-DQB1*03:06',
'HLA-DQA1*01:01-DQB1*03:07', 'HLA-DQA1*01:01-DQB1*03:08', 'HLA-DQA1*01:01-DQB1*03:09', 'HLA-DQA1*01:01-DQB1*03:10', 'HLA-DQA1*01:01-DQB1*03:11',
'HLA-DQA1*01:01-DQB1*03:12', 'HLA-DQA1*01:01-DQB1*03:13', 'HLA-DQA1*01:01-DQB1*03:14', 'HLA-DQA1*01:01-DQB1*03:15', 'HLA-DQA1*01:01-DQB1*03:16',
'HLA-DQA1*01:01-DQB1*03:17', 'HLA-DQA1*01:01-DQB1*03:18', 'HLA-DQA1*01:01-DQB1*03:19', 'HLA-DQA1*01:01-DQB1*03:20', 'HLA-DQA1*01:01-DQB1*03:21',
'HLA-DQA1*01:01-DQB1*03:22', 'HLA-DQA1*01:01-DQB1*03:23', 'HLA-DQA1*01:01-DQB1*03:24', 'HLA-DQA1*01:01-DQB1*03:25', 'HLA-DQA1*01:01-DQB1*03:26',
'HLA-DQA1*01:01-DQB1*03:27', 'HLA-DQA1*01:01-DQB1*03:28', 'HLA-DQA1*01:01-DQB1*03:29', 'HLA-DQA1*01:01-DQB1*03:30', 'HLA-DQA1*01:01-DQB1*03:31',
'HLA-DQA1*01:01-DQB1*03:32', 'HLA-DQA1*01:01-DQB1*03:33', 'HLA-DQA1*01:01-DQB1*03:34', 'HLA-DQA1*01:01-DQB1*03:35', 'HLA-DQA1*01:01-DQB1*03:36',
'HLA-DQA1*01:01-DQB1*03:37', 'HLA-DQA1*01:01-DQB1*03:38', 'HLA-DQA1*01:01-DQB1*04:01', 'HLA-DQA1*01:01-DQB1*04:02', 'HLA-DQA1*01:01-DQB1*04:03',
'HLA-DQA1*01:01-DQB1*04:04', 'HLA-DQA1*01:01-DQB1*04:05', 'HLA-DQA1*01:01-DQB1*04:06', 'HLA-DQA1*01:01-DQB1*04:07', 'HLA-DQA1*01:01-DQB1*04:08',
'HLA-DQA1*01:01-DQB1*05:01', 'HLA-DQA1*01:01-DQB1*05:02', 'HLA-DQA1*01:01-DQB1*05:03', 'HLA-DQA1*01:01-DQB1*05:05', 'HLA-DQA1*01:01-DQB1*05:06',
'HLA-DQA1*01:01-DQB1*05:07', 'HLA-DQA1*01:01-DQB1*05:08', 'HLA-DQA1*01:01-DQB1*05:09', 'HLA-DQA1*01:01-DQB1*05:10', 'HLA-DQA1*01:01-DQB1*05:11',
'HLA-DQA1*01:01-DQB1*05:12', 'HLA-DQA1*01:01-DQB1*05:13', 'HLA-DQA1*01:01-DQB1*05:14', 'HLA-DQA1*01:01-DQB1*06:01', 'HLA-DQA1*01:01-DQB1*06:02',
'HLA-DQA1*01:01-DQB1*06:03', 'HLA-DQA1*01:01-DQB1*06:04', 'HLA-DQA1*01:01-DQB1*06:07', 'HLA-DQA1*01:01-DQB1*06:08', 'HLA-DQA1*01:01-DQB1*06:09',
'HLA-DQA1*01:01-DQB1*06:10', 'HLA-DQA1*01:01-DQB1*06:11', 'HLA-DQA1*01:01-DQB1*06:12', 'HLA-DQA1*01:01-DQB1*06:14', 'HLA-DQA1*01:01-DQB1*06:15',
'HLA-DQA1*01:01-DQB1*06:16', 'HLA-DQA1*01:01-DQB1*06:17', 'HLA-DQA1*01:01-DQB1*06:18', 'HLA-DQA1*01:01-DQB1*06:19', 'HLA-DQA1*01:01-DQB1*06:21',
'HLA-DQA1*01:01-DQB1*06:22', 'HLA-DQA1*01:01-DQB1*06:23', 'HLA-DQA1*01:01-DQB1*06:24', 'HLA-DQA1*01:01-DQB1*06:25', 'HLA-DQA1*01:01-DQB1*06:27',
'HLA-DQA1*01:01-DQB1*06:28', 'HLA-DQA1*01:01-DQB1*06:29', 'HLA-DQA1*01:01-DQB1*06:30', 'HLA-DQA1*01:01-DQB1*06:31', 'HLA-DQA1*01:01-DQB1*06:32',
'HLA-DQA1*01:01-DQB1*06:33', 'HLA-DQA1*01:01-DQB1*06:34', 'HLA-DQA1*01:01-DQB1*06:35', 'HLA-DQA1*01:01-DQB1*06:36', 'HLA-DQA1*01:01-DQB1*06:37',
'HLA-DQA1*01:01-DQB1*06:38', 'HLA-DQA1*01:01-DQB1*06:39', 'HLA-DQA1*01:01-DQB1*06:40', 'HLA-DQA1*01:01-DQB1*06:41', 'HLA-DQA1*01:01-DQB1*06:42',
'HLA-DQA1*01:01-DQB1*06:43', 'HLA-DQA1*01:01-DQB1*06:44', 'HLA-DQA1*01:02-DQB1*02:01', 'HLA-DQA1*01:02-DQB1*02:02', 'HLA-DQA1*01:02-DQB1*02:03',
'HLA-DQA1*01:02-DQB1*02:04', 'HLA-DQA1*01:02-DQB1*02:05', 'HLA-DQA1*01:02-DQB1*02:06', 'HLA-DQA1*01:02-DQB1*03:01', 'HLA-DQA1*01:02-DQB1*03:02',
'HLA-DQA1*01:02-DQB1*03:03', 'HLA-DQA1*01:02-DQB1*03:04', 'HLA-DQA1*01:02-DQB1*03:05', 'HLA-DQA1*01:02-DQB1*03:06', 'HLA-DQA1*01:02-DQB1*03:07',
'HLA-DQA1*01:02-DQB1*03:08', 'HLA-DQA1*01:02-DQB1*03:09', 'HLA-DQA1*01:02-DQB1*03:10', 'HLA-DQA1*01:02-DQB1*03:11', 'HLA-DQA1*01:02-DQB1*03:12',
'HLA-DQA1*01:02-DQB1*03:13', 'HLA-DQA1*01:02-DQB1*03:14', 'HLA-DQA1*01:02-DQB1*03:15', 'HLA-DQA1*01:02-DQB1*03:16', 'HLA-DQA1*01:02-DQB1*03:17',
'HLA-DQA1*01:02-DQB1*03:18', 'HLA-DQA1*01:02-DQB1*03:19', 'HLA-DQA1*01:02-DQB1*03:20', 'HLA-DQA1*01:02-DQB1*03:21', 'HLA-DQA1*01:02-DQB1*03:22',
'HLA-DQA1*01:02-DQB1*03:23', 'HLA-DQA1*01:02-DQB1*03:24', 'HLA-DQA1*01:02-DQB1*03:25', 'HLA-DQA1*01:02-DQB1*03:26', 'HLA-DQA1*01:02-DQB1*03:27',
'HLA-DQA1*01:02-DQB1*03:28', 'HLA-DQA1*01:02-DQB1*03:29', 'HLA-DQA1*01:02-DQB1*03:30', 'HLA-DQA1*01:02-DQB1*03:31', 'HLA-DQA1*01:02-DQB1*03:32',
'HLA-DQA1*01:02-DQB1*03:33', 'HLA-DQA1*01:02-DQB1*03:34', 'HLA-DQA1*01:02-DQB1*03:35', 'HLA-DQA1*01:02-DQB1*03:36', 'HLA-DQA1*01:02-DQB1*03:37',
'HLA-DQA1*01:02-DQB1*03:38', 'HLA-DQA1*01:02-DQB1*04:01', 'HLA-DQA1*01:02-DQB1*04:02', 'HLA-DQA1*01:02-DQB1*04:03', 'HLA-DQA1*01:02-DQB1*04:04',
'HLA-DQA1*01:02-DQB1*04:05', 'HLA-DQA1*01:02-DQB1*04:06', 'HLA-DQA1*01:02-DQB1*04:07', 'HLA-DQA1*01:02-DQB1*04:08', 'HLA-DQA1*01:02-DQB1*05:01',
'HLA-DQA1*01:02-DQB1*05:02', 'HLA-DQA1*01:02-DQB1*05:03', 'HLA-DQA1*01:02-DQB1*05:05', 'HLA-DQA1*01:02-DQB1*05:06', 'HLA-DQA1*01:02-DQB1*05:07',
'HLA-DQA1*01:02-DQB1*05:08', 'HLA-DQA1*01:02-DQB1*05:09', 'HLA-DQA1*01:02-DQB1*05:10', 'HLA-DQA1*01:02-DQB1*05:11', 'HLA-DQA1*01:02-DQB1*05:12',
'HLA-DQA1*01:02-DQB1*05:13', 'HLA-DQA1*01:02-DQB1*05:14', 'HLA-DQA1*01:02-DQB1*06:01', 'HLA-DQA1*01:02-DQB1*06:02', 'HLA-DQA1*01:02-DQB1*06:03',
'HLA-DQA1*01:02-DQB1*06:04', 'HLA-DQA1*01:02-DQB1*06:07', 'HLA-DQA1*01:02-DQB1*06:08', 'HLA-DQA1*01:02-DQB1*06:09', 'HLA-DQA1*01:02-DQB1*06:10',
'HLA-DQA1*01:02-DQB1*06:11', 'HLA-DQA1*01:02-DQB1*06:12', 'HLA-DQA1*01:02-DQB1*06:14', 'HLA-DQA1*01:02-DQB1*06:15', 'HLA-DQA1*01:02-DQB1*06:16',
'HLA-DQA1*01:02-DQB1*06:17', 'HLA-DQA1*01:02-DQB1*06:18', 'HLA-DQA1*01:02-DQB1*06:19', 'HLA-DQA1*01:02-DQB1*06:21', 'HLA-DQA1*01:02-DQB1*06:22',
'HLA-DQA1*01:02-DQB1*06:23', 'HLA-DQA1*01:02-DQB1*06:24', 'HLA-DQA1*01:02-DQB1*06:25', 'HLA-DQA1*01:02-DQB1*06:27', 'HLA-DQA1*01:02-DQB1*06:28',
'HLA-DQA1*01:02-DQB1*06:29', 'HLA-DQA1*01:02-DQB1*06:30', 'HLA-DQA1*01:02-DQB1*06:31', 'HLA-DQA1*01:02-DQB1*06:32', 'HLA-DQA1*01:02-DQB1*06:33',
'HLA-DQA1*01:02-DQB1*06:34', 'HLA-DQA1*01:02-DQB1*06:35', 'HLA-DQA1*01:02-DQB1*06:36', 'HLA-DQA1*01:02-DQB1*06:37', 'HLA-DQA1*01:02-DQB1*06:38',
'HLA-DQA1*01:02-DQB1*06:39', 'HLA-DQA1*01:02-DQB1*06:40', 'HLA-DQA1*01:02-DQB1*06:41', 'HLA-DQA1*01:02-DQB1*06:42', 'HLA-DQA1*01:02-DQB1*06:43',
'HLA-DQA1*01:02-DQB1*06:44', 'HLA-DQA1*01:03-DQB1*02:01', 'HLA-DQA1*01:03-DQB1*02:02', 'HLA-DQA1*01:03-DQB1*02:03', 'HLA-DQA1*01:03-DQB1*02:04',
'HLA-DQA1*01:03-DQB1*02:05', 'HLA-DQA1*01:03-DQB1*02:06', 'HLA-DQA1*01:03-DQB1*03:01', 'HLA-DQA1*01:03-DQB1*03:02', 'HLA-DQA1*01:03-DQB1*03:03',
'HLA-DQA1*01:03-DQB1*03:04', 'HLA-DQA1*01:03-DQB1*03:05', 'HLA-DQA1*01:03-DQB1*03:06', 'HLA-DQA1*01:03-DQB1*03:07', 'HLA-DQA1*01:03-DQB1*03:08',
'HLA-DQA1*01:03-DQB1*03:09', 'HLA-DQA1*01:03-DQB1*03:10', 'HLA-DQA1*01:03-DQB1*03:11', 'HLA-DQA1*01:03-DQB1*03:12', 'HLA-DQA1*01:03-DQB1*03:13',
'HLA-DQA1*01:03-DQB1*03:14', 'HLA-DQA1*01:03-DQB1*03:15', 'HLA-DQA1*01:03-DQB1*03:16', 'HLA-DQA1*01:03-DQB1*03:17', 'HLA-DQA1*01:03-DQB1*03:18',
'HLA-DQA1*01:03-DQB1*03:19', 'HLA-DQA1*01:03-DQB1*03:20', 'HLA-DQA1*01:03-DQB1*03:21', 'HLA-DQA1*01:03-DQB1*03:22', 'HLA-DQA1*01:03-DQB1*03:23',
'HLA-DQA1*01:03-DQB1*03:24', 'HLA-DQA1*01:03-DQB1*03:25', 'HLA-DQA1*01:03-DQB1*03:26', 'HLA-DQA1*01:03-DQB1*03:27', 'HLA-DQA1*01:03-DQB1*03:28',
'HLA-DQA1*01:03-DQB1*03:29', 'HLA-DQA1*01:03-DQB1*03:30', 'HLA-DQA1*01:03-DQB1*03:31', 'HLA-DQA1*01:03-DQB1*03:32', 'HLA-DQA1*01:03-DQB1*03:33',
'HLA-DQA1*01:03-DQB1*03:34', 'HLA-DQA1*01:03-DQB1*03:35', 'HLA-DQA1*01:03-DQB1*03:36', 'HLA-DQA1*01:03-DQB1*03:37', 'HLA-DQA1*01:03-DQB1*03:38',
'HLA-DQA1*01:03-DQB1*04:01', 'HLA-DQA1*01:03-DQB1*04:02', 'HLA-DQA1*01:03-DQB1*04:03', 'HLA-DQA1*01:03-DQB1*04:04', 'HLA-DQA1*01:03-DQB1*04:05',
'HLA-DQA1*01:03-DQB1*04:06', 'HLA-DQA1*01:03-DQB1*04:07', 'HLA-DQA1*01:03-DQB1*04:08', 'HLA-DQA1*01:03-DQB1*05:01', 'HLA-DQA1*01:03-DQB1*05:02',
'HLA-DQA1*01:03-DQB1*05:03', 'HLA-DQA1*01:03-DQB1*05:05', 'HLA-DQA1*01:03-DQB1*05:06', 'HLA-DQA1*01:03-DQB1*05:07', 'HLA-DQA1*01:03-DQB1*05:08',
'HLA-DQA1*01:03-DQB1*05:09', 'HLA-DQA1*01:03-DQB1*05:10', 'HLA-DQA1*01:03-DQB1*05:11', 'HLA-DQA1*01:03-DQB1*05:12', 'HLA-DQA1*01:03-DQB1*05:13',
'HLA-DQA1*01:03-DQB1*05:14', 'HLA-DQA1*01:03-DQB1*06:01', 'HLA-DQA1*01:03-DQB1*06:02', 'HLA-DQA1*01:03-DQB1*06:03', 'HLA-DQA1*01:03-DQB1*06:04',
'HLA-DQA1*01:03-DQB1*06:07', 'HLA-DQA1*01:03-DQB1*06:08', 'HLA-DQA1*01:03-DQB1*06:09', 'HLA-DQA1*01:03-DQB1*06:10', 'HLA-DQA1*01:03-DQB1*06:11',
'HLA-DQA1*01:03-DQB1*06:12', 'HLA-DQA1*01:03-DQB1*06:14', 'HLA-DQA1*01:03-DQB1*06:15', 'HLA-DQA1*01:03-DQB1*06:16', 'HLA-DQA1*01:03-DQB1*06:17',
'HLA-DQA1*01:03-DQB1*06:18', 'HLA-DQA1*01:03-DQB1*06:19', 'HLA-DQA1*01:03-DQB1*06:21', 'HLA-DQA1*01:03-DQB1*06:22', 'HLA-DQA1*01:03-DQB1*06:23',
'HLA-DQA1*01:03-DQB1*06:24', 'HLA-DQA1*01:03-DQB1*06:25', 'HLA-DQA1*01:03-DQB1*06:27', 'HLA-DQA1*01:03-DQB1*06:28', 'HLA-DQA1*01:03-DQB1*06:29',
'HLA-DQA1*01:03-DQB1*06:30', 'HLA-DQA1*01:03-DQB1*06:31', 'HLA-DQA1*01:03-DQB1*06:32', 'HLA-DQA1*01:03-DQB1*06:33', 'HLA-DQA1*01:03-DQB1*06:34',
'HLA-DQA1*01:03-DQB1*06:35', 'HLA-DQA1*01:03-DQB1*06:36', 'HLA-DQA1*01:03-DQB1*06:37', 'HLA-DQA1*01:03-DQB1*06:38', 'HLA-DQA1*01:03-DQB1*06:39',
'HLA-DQA1*01:03-DQB1*06:40', 'HLA-DQA1*01:03-DQB1*06:41', 'HLA-DQA1*01:03-DQB1*06:42', 'HLA-DQA1*01:03-DQB1*06:43', 'HLA-DQA1*01:03-DQB1*06:44',
'HLA-DQA1*01:04-DQB1*02:01', 'HLA-DQA1*01:04-DQB1*02:02', 'HLA-DQA1*01:04-DQB1*02:03', 'HLA-DQA1*01:04-DQB1*02:04', 'HLA-DQA1*01:04-DQB1*02:05',
'HLA-DQA1*01:04-DQB1*02:06', 'HLA-DQA1*01:04-DQB1*03:01', 'HLA-DQA1*01:04-DQB1*03:02', 'HLA-DQA1*01:04-DQB1*03:03', 'HLA-DQA1*01:04-DQB1*03:04',
'HLA-DQA1*01:04-DQB1*03:05', 'HLA-DQA1*01:04-DQB1*03:06', 'HLA-DQA1*01:04-DQB1*03:07', 'HLA-DQA1*01:04-DQB1*03:08', 'HLA-DQA1*01:04-DQB1*03:09',
'HLA-DQA1*01:04-DQB1*03:10', 'HLA-DQA1*01:04-DQB1*03:11', 'HLA-DQA1*01:04-DQB1*03:12', 'HLA-DQA1*01:04-DQB1*03:13', 'HLA-DQA1*01:04-DQB1*03:14',
'HLA-DQA1*01:04-DQB1*03:15', 'HLA-DQA1*01:04-DQB1*03:16', 'HLA-DQA1*01:04-DQB1*03:17', 'HLA-DQA1*01:04-DQB1*03:18', 'HLA-DQA1*01:04-DQB1*03:19',
'HLA-DQA1*01:04-DQB1*03:20', 'HLA-DQA1*01:04-DQB1*03:21', 'HLA-DQA1*01:04-DQB1*03:22', 'HLA-DQA1*01:04-DQB1*03:23', 'HLA-DQA1*01:04-DQB1*03:24',
'HLA-DQA1*01:04-DQB1*03:25', 'HLA-DQA1*01:04-DQB1*03:26', 'HLA-DQA1*01:04-DQB1*03:27', 'HLA-DQA1*01:04-DQB1*03:28', 'HLA-DQA1*01:04-DQB1*03:29',
'HLA-DQA1*01:04-DQB1*03:30', 'HLA-DQA1*01:04-DQB1*03:31', 'HLA-DQA1*01:04-DQB1*03:32', 'HLA-DQA1*01:04-DQB1*03:33', 'HLA-DQA1*01:04-DQB1*03:34',
'HLA-DQA1*01:04-DQB1*03:35', 'HLA-DQA1*01:04-DQB1*03:36', 'HLA-DQA1*01:04-DQB1*03:37', 'HLA-DQA1*01:04-DQB1*03:38', 'HLA-DQA1*01:04-DQB1*04:01',
'HLA-DQA1*01:04-DQB1*04:02', 'HLA-DQA1*01:04-DQB1*04:03', 'HLA-DQA1*01:04-DQB1*04:04', 'HLA-DQA1*01:04-DQB1*04:05', 'HLA-DQA1*01:04-DQB1*04:06',
'HLA-DQA1*01:04-DQB1*04:07', 'HLA-DQA1*01:04-DQB1*04:08', 'HLA-DQA1*01:04-DQB1*05:01', 'HLA-DQA1*01:04-DQB1*05:02', 'HLA-DQA1*01:04-DQB1*05:03',
'HLA-DQA1*01:04-DQB1*05:05', 'HLA-DQA1*01:04-DQB1*05:06', 'HLA-DQA1*01:04-DQB1*05:07', 'HLA-DQA1*01:04-DQB1*05:08', 'HLA-DQA1*01:04-DQB1*05:09',
'HLA-DQA1*01:04-DQB1*05:10', 'HLA-DQA1*01:04-DQB1*05:11', 'HLA-DQA1*01:04-DQB1*05:12', 'HLA-DQA1*01:04-DQB1*05:13', 'HLA-DQA1*01:04-DQB1*05:14',
'HLA-DQA1*01:04-DQB1*06:01', 'HLA-DQA1*01:04-DQB1*06:02', 'HLA-DQA1*01:04-DQB1*06:03', 'HLA-DQA1*01:04-DQB1*06:04', 'HLA-DQA1*01:04-DQB1*06:07',
'HLA-DQA1*01:04-DQB1*06:08', 'HLA-DQA1*01:04-DQB1*06:09', 'HLA-DQA1*01:04-DQB1*06:10', 'HLA-DQA1*01:04-DQB1*06:11', 'HLA-DQA1*01:04-DQB1*06:12',
'HLA-DQA1*01:04-DQB1*06:14', 'HLA-DQA1*01:04-DQB1*06:15', 'HLA-DQA1*01:04-DQB1*06:16', 'HLA-DQA1*01:04-DQB1*06:17', 'HLA-DQA1*01:04-DQB1*06:18',
'HLA-DQA1*01:04-DQB1*06:19', 'HLA-DQA1*01:04-DQB1*06:21', 'HLA-DQA1*01:04-DQB1*06:22', 'HLA-DQA1*01:04-DQB1*06:23', 'HLA-DQA1*01:04-DQB1*06:24',
'HLA-DQA1*01:04-DQB1*06:25', 'HLA-DQA1*01:04-DQB1*06:27', 'HLA-DQA1*01:04-DQB1*06:28', 'HLA-DQA1*01:04-DQB1*06:29', 'HLA-DQA1*01:04-DQB1*06:30',
'HLA-DQA1*01:04-DQB1*06:31', 'HLA-DQA1*01:04-DQB1*06:32', 'HLA-DQA1*01:04-DQB1*06:33', 'HLA-DQA1*01:04-DQB1*06:34', 'HLA-DQA1*01:04-DQB1*06:35',
'HLA-DQA1*01:04-DQB1*06:36', 'HLA-DQA1*01:04-DQB1*06:37', 'HLA-DQA1*01:04-DQB1*06:38', 'HLA-DQA1*01:04-DQB1*06:39', 'HLA-DQA1*01:04-DQB1*06:40',
'HLA-DQA1*01:04-DQB1*06:41', 'HLA-DQA1*01:04-DQB1*06:42', 'HLA-DQA1*01:04-DQB1*06:43', 'HLA-DQA1*01:04-DQB1*06:44', 'HLA-DQA1*01:05-DQB1*02:01',
'HLA-DQA1*01:05-DQB1*02:02', 'HLA-DQA1*01:05-DQB1*02:03', 'HLA-DQA1*01:05-DQB1*02:04', 'HLA-DQA1*01:05-DQB1*02:05', 'HLA-DQA1*01:05-DQB1*02:06',
'HLA-DQA1*01:05-DQB1*03:01', 'HLA-DQA1*01:05-DQB1*03:02', 'HLA-DQA1*01:05-DQB1*03:03', 'HLA-DQA1*01:05-DQB1*03:04', 'HLA-DQA1*01:05-DQB1*03:05',
'HLA-DQA1*01:05-DQB1*03:06', 'HLA-DQA1*01:05-DQB1*03:07', 'HLA-DQA1*01:05-DQB1*03:08', 'HLA-DQA1*01:05-DQB1*03:09', 'HLA-DQA1*01:05-DQB1*03:10',
'HLA-DQA1*01:05-DQB1*03:11', 'HLA-DQA1*01:05-DQB1*03:12', 'HLA-DQA1*01:05-DQB1*03:13', 'HLA-DQA1*01:05-DQB1*03:14', 'HLA-DQA1*01:05-DQB1*03:15',
'HLA-DQA1*01:05-DQB1*03:16', 'HLA-DQA1*01:05-DQB1*03:17', 'HLA-DQA1*01:05-DQB1*03:18', 'HLA-DQA1*01:05-DQB1*03:19', 'HLA-DQA1*01:05-DQB1*03:20',
'HLA-DQA1*01:05-DQB1*03:21', 'HLA-DQA1*01:05-DQB1*03:22', 'HLA-DQA1*01:05-DQB1*03:23', 'HLA-DQA1*01:05-DQB1*03:24', 'HLA-DQA1*01:05-DQB1*03:25',
'HLA-DQA1*01:05-DQB1*03:26', 'HLA-DQA1*01:05-DQB1*03:27', 'HLA-DQA1*01:05-DQB1*03:28', 'HLA-DQA1*01:05-DQB1*03:29', 'HLA-DQA1*01:05-DQB1*03:30',
'HLA-DQA1*01:05-DQB1*03:31', 'HLA-DQA1*01:05-DQB1*03:32', 'HLA-DQA1*01:05-DQB1*03:33', 'HLA-DQA1*01:05-DQB1*03:34', 'HLA-DQA1*01:05-DQB1*03:35',
'HLA-DQA1*01:05-DQB1*03:36', 'HLA-DQA1*01:05-DQB1*03:37', 'HLA-DQA1*01:05-DQB1*03:38', 'HLA-DQA1*01:05-DQB1*04:01', 'HLA-DQA1*01:05-DQB1*04:02',
'HLA-DQA1*01:05-DQB1*04:03', 'HLA-DQA1*01:05-DQB1*04:04', 'HLA-DQA1*01:05-DQB1*04:05', 'HLA-DQA1*01:05-DQB1*04:06', 'HLA-DQA1*01:05-DQB1*04:07',
'HLA-DQA1*01:05-DQB1*04:08', 'HLA-DQA1*01:05-DQB1*05:01', 'HLA-DQA1*01:05-DQB1*05:02', 'HLA-DQA1*01:05-DQB1*05:03', 'HLA-DQA1*01:05-DQB1*05:05',
'HLA-DQA1*01:05-DQB1*05:06', 'HLA-DQA1*01:05-DQB1*05:07', 'HLA-DQA1*01:05-DQB1*05:08', 'HLA-DQA1*01:05-DQB1*05:09', 'HLA-DQA1*01:05-DQB1*05:10',
'HLA-DQA1*01:05-DQB1*05:11', 'HLA-DQA1*01:05-DQB1*05:12', 'HLA-DQA1*01:05-DQB1*05:13', 'HLA-DQA1*01:05-DQB1*05:14', 'HLA-DQA1*01:05-DQB1*06:01',
'HLA-DQA1*01:05-DQB1*06:02', 'HLA-DQA1*01:05-DQB1*06:03', 'HLA-DQA1*01:05-DQB1*06:04', 'HLA-DQA1*01:05-DQB1*06:07', 'HLA-DQA1*01:05-DQB1*06:08',
'HLA-DQA1*01:05-DQB1*06:09', 'HLA-DQA1*01:05-DQB1*06:10', 'HLA-DQA1*01:05-DQB1*06:11', 'HLA-DQA1*01:05-DQB1*06:12', 'HLA-DQA1*01:05-DQB1*06:14',
'HLA-DQA1*01:05-DQB1*06:15', 'HLA-DQA1*01:05-DQB1*06:16', 'HLA-DQA1*01:05-DQB1*06:17', 'HLA-DQA1*01:05-DQB1*06:18', 'HLA-DQA1*01:05-DQB1*06:19',
'HLA-DQA1*01:05-DQB1*06:21', 'HLA-DQA1*01:05-DQB1*06:22', 'HLA-DQA1*01:05-DQB1*06:23', 'HLA-DQA1*01:05-DQB1*06:24', 'HLA-DQA1*01:05-DQB1*06:25',
'HLA-DQA1*01:05-DQB1*06:27', 'HLA-DQA1*01:05-DQB1*06:28', 'HLA-DQA1*01:05-DQB1*06:29', 'HLA-DQA1*01:05-DQB1*06:30', 'HLA-DQA1*01:05-DQB1*06:31',
'HLA-DQA1*01:05-DQB1*06:32', 'HLA-DQA1*01:05-DQB1*06:33', 'HLA-DQA1*01:05-DQB1*06:34', 'HLA-DQA1*01:05-DQB1*06:35', 'HLA-DQA1*01:05-DQB1*06:36',
'HLA-DQA1*01:05-DQB1*06:37', 'HLA-DQA1*01:05-DQB1*06:38', 'HLA-DQA1*01:05-DQB1*06:39', 'HLA-DQA1*01:05-DQB1*06:40', 'HLA-DQA1*01:05-DQB1*06:41',
'HLA-DQA1*01:05-DQB1*06:42', 'HLA-DQA1*01:05-DQB1*06:43', 'HLA-DQA1*01:05-DQB1*06:44', 'HLA-DQA1*01:06-DQB1*02:01', 'HLA-DQA1*01:06-DQB1*02:02',
'HLA-DQA1*01:06-DQB1*02:03', 'HLA-DQA1*01:06-DQB1*02:04', 'HLA-DQA1*01:06-DQB1*02:05', 'HLA-DQA1*01:06-DQB1*02:06', 'HLA-DQA1*01:06-DQB1*03:01',
'HLA-DQA1*01:06-DQB1*03:02', 'HLA-DQA1*01:06-DQB1*03:03', 'HLA-DQA1*01:06-DQB1*03:04', 'HLA-DQA1*01:06-DQB1*03:05', 'HLA-DQA1*01:06-DQB1*03:06',
'HLA-DQA1*01:06-DQB1*03:07', 'HLA-DQA1*01:06-DQB1*03:08', 'HLA-DQA1*01:06-DQB1*03:09', 'HLA-DQA1*01:06-DQB1*03:10', 'HLA-DQA1*01:06-DQB1*03:11',
'HLA-DQA1*01:06-DQB1*03:12', 'HLA-DQA1*01:06-DQB1*03:13', 'HLA-DQA1*01:06-DQB1*03:14', 'HLA-DQA1*01:06-DQB1*03:15', 'HLA-DQA1*01:06-DQB1*03:16',
'HLA-DQA1*01:06-DQB1*03:17', 'HLA-DQA1*01:06-DQB1*03:18', 'HLA-DQA1*01:06-DQB1*03:19', 'HLA-DQA1*01:06-DQB1*03:20', 'HLA-DQA1*01:06-DQB1*03:21',
'HLA-DQA1*01:06-DQB1*03:22', 'HLA-DQA1*01:06-DQB1*03:23', 'HLA-DQA1*01:06-DQB1*03:24', 'HLA-DQA1*01:06-DQB1*03:25', 'HLA-DQA1*01:06-DQB1*03:26',
'HLA-DQA1*01:06-DQB1*03:27', 'HLA-DQA1*01:06-DQB1*03:28', 'HLA-DQA1*01:06-DQB1*03:29', 'HLA-DQA1*01:06-DQB1*03:30', 'HLA-DQA1*01:06-DQB1*03:31',
'HLA-DQA1*01:06-DQB1*03:32', 'HLA-DQA1*01:06-DQB1*03:33', 'HLA-DQA1*01:06-DQB1*03:34', 'HLA-DQA1*01:06-DQB1*03:35', 'HLA-DQA1*01:06-DQB1*03:36',
'HLA-DQA1*01:06-DQB1*03:37', 'HLA-DQA1*01:06-DQB1*03:38', 'HLA-DQA1*01:06-DQB1*04:01', 'HLA-DQA1*01:06-DQB1*04:02', 'HLA-DQA1*01:06-DQB1*04:03',
'HLA-DQA1*01:06-DQB1*04:04', 'HLA-DQA1*01:06-DQB1*04:05', 'HLA-DQA1*01:06-DQB1*04:06', 'HLA-DQA1*01:06-DQB1*04:07', 'HLA-DQA1*01:06-DQB1*04:08',
'HLA-DQA1*01:06-DQB1*05:01', 'HLA-DQA1*01:06-DQB1*05:02', 'HLA-DQA1*01:06-DQB1*05:03', 'HLA-DQA1*01:06-DQB1*05:05', 'HLA-DQA1*01:06-DQB1*05:06',
'HLA-DQA1*01:06-DQB1*05:07', 'HLA-DQA1*01:06-DQB1*05:08', 'HLA-DQA1*01:06-DQB1*05:09', 'HLA-DQA1*01:06-DQB1*05:10', 'HLA-DQA1*01:06-DQB1*05:11',
'HLA-DQA1*01:06-DQB1*05:12', 'HLA-DQA1*01:06-DQB1*05:13', 'HLA-DQA1*01:06-DQB1*05:14', 'HLA-DQA1*01:06-DQB1*06:01', 'HLA-DQA1*01:06-DQB1*06:02',
'HLA-DQA1*01:06-DQB1*06:03', 'HLA-DQA1*01:06-DQB1*06:04', 'HLA-DQA1*01:06-DQB1*06:07', 'HLA-DQA1*01:06-DQB1*06:08', 'HLA-DQA1*01:06-DQB1*06:09',
'HLA-DQA1*01:06-DQB1*06:10', 'HLA-DQA1*01:06-DQB1*06:11', 'HLA-DQA1*01:06-DQB1*06:12', 'HLA-DQA1*01:06-DQB1*06:14', 'HLA-DQA1*01:06-DQB1*06:15',
'HLA-DQA1*01:06-DQB1*06:16', 'HLA-DQA1*01:06-DQB1*06:17', 'HLA-DQA1*01:06-DQB1*06:18', 'HLA-DQA1*01:06-DQB1*06:19', 'HLA-DQA1*01:06-DQB1*06:21',
'HLA-DQA1*01:06-DQB1*06:22', 'HLA-DQA1*01:06-DQB1*06:23', 'HLA-DQA1*01:06-DQB1*06:24', 'HLA-DQA1*01:06-DQB1*06:25', 'HLA-DQA1*01:06-DQB1*06:27',
'HLA-DQA1*01:06-DQB1*06:28', 'HLA-DQA1*01:06-DQB1*06:29', 'HLA-DQA1*01:06-DQB1*06:30', 'HLA-DQA1*01:06-DQB1*06:31', 'HLA-DQA1*01:06-DQB1*06:32',
'HLA-DQA1*01:06-DQB1*06:33', 'HLA-DQA1*01:06-DQB1*06:34', 'HLA-DQA1*01:06-DQB1*06:35', 'HLA-DQA1*01:06-DQB1*06:36', 'HLA-DQA1*01:06-DQB1*06:37',
'HLA-DQA1*01:06-DQB1*06:38', 'HLA-DQA1*01:06-DQB1*06:39', 'HLA-DQA1*01:06-DQB1*06:40', 'HLA-DQA1*01:06-DQB1*06:41', 'HLA-DQA1*01:06-DQB1*06:42',
'HLA-DQA1*01:06-DQB1*06:43', 'HLA-DQA1*01:06-DQB1*06:44', 'HLA-DQA1*01:07-DQB1*02:01', 'HLA-DQA1*01:07-DQB1*02:02', 'HLA-DQA1*01:07-DQB1*02:03',
'HLA-DQA1*01:07-DQB1*02:04', 'HLA-DQA1*01:07-DQB1*02:05', 'HLA-DQA1*01:07-DQB1*02:06', 'HLA-DQA1*01:07-DQB1*03:01', 'HLA-DQA1*01:07-DQB1*03:02',
'HLA-DQA1*01:07-DQB1*03:03', 'HLA-DQA1*01:07-DQB1*03:04', 'HLA-DQA1*01:07-DQB1*03:05', 'HLA-DQA1*01:07-DQB1*03:06', 'HLA-DQA1*01:07-DQB1*03:07',
'HLA-DQA1*01:07-DQB1*03:08', 'HLA-DQA1*01:07-DQB1*03:09', 'HLA-DQA1*01:07-DQB1*03:10', 'HLA-DQA1*01:07-DQB1*03:11', 'HLA-DQA1*01:07-DQB1*03:12',
'HLA-DQA1*01:07-DQB1*03:13', 'HLA-DQA1*01:07-DQB1*03:14', 'HLA-DQA1*01:07-DQB1*03:15', 'HLA-DQA1*01:07-DQB1*03:16', 'HLA-DQA1*01:07-DQB1*03:17',
'HLA-DQA1*01:07-DQB1*03:18', 'HLA-DQA1*01:07-DQB1*03:19', 'HLA-DQA1*01:07-DQB1*03:20', 'HLA-DQA1*01:07-DQB1*03:21', 'HLA-DQA1*01:07-DQB1*03:22',
'HLA-DQA1*01:07-DQB1*03:23', 'HLA-DQA1*01:07-DQB1*03:24', 'HLA-DQA1*01:07-DQB1*03:25', 'HLA-DQA1*01:07-DQB1*03:26', 'HLA-DQA1*01:07-DQB1*03:27',
'HLA-DQA1*01:07-DQB1*03:28', 'HLA-DQA1*01:07-DQB1*03:29', 'HLA-DQA1*01:07-DQB1*03:30', 'HLA-DQA1*01:07-DQB1*03:31', 'HLA-DQA1*01:07-DQB1*03:32',
'HLA-DQA1*01:07-DQB1*03:33', 'HLA-DQA1*01:07-DQB1*03:34', 'HLA-DQA1*01:07-DQB1*03:35', 'HLA-DQA1*01:07-DQB1*03:36', 'HLA-DQA1*01:07-DQB1*03:37',
'HLA-DQA1*01:07-DQB1*03:38', 'HLA-DQA1*01:07-DQB1*04:01', 'HLA-DQA1*01:07-DQB1*04:02', 'HLA-DQA1*01:07-DQB1*04:03', 'HLA-DQA1*01:07-DQB1*04:04',
'HLA-DQA1*01:07-DQB1*04:05', 'HLA-DQA1*01:07-DQB1*04:06', 'HLA-DQA1*01:07-DQB1*04:07', 'HLA-DQA1*01:07-DQB1*04:08', 'HLA-DQA1*01:07-DQB1*05:01',
'HLA-DQA1*01:07-DQB1*05:02', 'HLA-DQA1*01:07-DQB1*05:03', 'HLA-DQA1*01:07-DQB1*05:05', 'HLA-DQA1*01:07-DQB1*05:06', 'HLA-DQA1*01:07-DQB1*05:07',
'HLA-DQA1*01:07-DQB1*05:08', 'HLA-DQA1*01:07-DQB1*05:09', 'HLA-DQA1*01:07-DQB1*05:10', 'HLA-DQA1*01:07-DQB1*05:11', 'HLA-DQA1*01:07-DQB1*05:12',
'HLA-DQA1*01:07-DQB1*05:13', 'HLA-DQA1*01:07-DQB1*05:14', 'HLA-DQA1*01:07-DQB1*06:01', 'HLA-DQA1*01:07-DQB1*06:02', 'HLA-DQA1*01:07-DQB1*06:03',
'HLA-DQA1*01:07-DQB1*06:04', 'HLA-DQA1*01:07-DQB1*06:07', 'HLA-DQA1*01:07-DQB1*06:08', 'HLA-DQA1*01:07-DQB1*06:09', 'HLA-DQA1*01:07-DQB1*06:10',
'HLA-DQA1*01:07-DQB1*06:11', 'HLA-DQA1*01:07-DQB1*06:12', 'HLA-DQA1*01:07-DQB1*06:14', 'HLA-DQA1*01:07-DQB1*06:15', 'HLA-DQA1*01:07-DQB1*06:16',
'HLA-DQA1*01:07-DQB1*06:17', 'HLA-DQA1*01:07-DQB1*06:18', 'HLA-DQA1*01:07-DQB1*06:19', 'HLA-DQA1*01:07-DQB1*06:21', 'HLA-DQA1*01:07-DQB1*06:22',
'HLA-DQA1*01:07-DQB1*06:23', 'HLA-DQA1*01:07-DQB1*06:24', 'HLA-DQA1*01:07-DQB1*06:25', 'HLA-DQA1*01:07-DQB1*06:27', 'HLA-DQA1*01:07-DQB1*06:28',
'HLA-DQA1*01:07-DQB1*06:29', 'HLA-DQA1*01:07-DQB1*06:30', 'HLA-DQA1*01:07-DQB1*06:31', 'HLA-DQA1*01:07-DQB1*06:32', 'HLA-DQA1*01:07-DQB1*06:33',
'HLA-DQA1*01:07-DQB1*06:34', 'HLA-DQA1*01:07-DQB1*06:35', 'HLA-DQA1*01:07-DQB1*06:36', 'HLA-DQA1*01:07-DQB1*06:37', 'HLA-DQA1*01:07-DQB1*06:38',
'HLA-DQA1*01:07-DQB1*06:39', 'HLA-DQA1*01:07-DQB1*06:40', 'HLA-DQA1*01:07-DQB1*06:41', 'HLA-DQA1*01:07-DQB1*06:42', 'HLA-DQA1*01:07-DQB1*06:43',
'HLA-DQA1*01:07-DQB1*06:44', 'HLA-DQA1*01:08-DQB1*02:01', 'HLA-DQA1*01:08-DQB1*02:02', 'HLA-DQA1*01:08-DQB1*02:03', 'HLA-DQA1*01:08-DQB1*02:04',
'HLA-DQA1*01:08-DQB1*02:05', 'HLA-DQA1*01:08-DQB1*02:06', 'HLA-DQA1*01:08-DQB1*03:01', 'HLA-DQA1*01:08-DQB1*03:02', 'HLA-DQA1*01:08-DQB1*03:03',
'HLA-DQA1*01:08-DQB1*03:04', 'HLA-DQA1*01:08-DQB1*03:05', 'HLA-DQA1*01:08-DQB1*03:06', 'HLA-DQA1*01:08-DQB1*03:07', 'HLA-DQA1*01:08-DQB1*03:08',
'HLA-DQA1*01:08-DQB1*03:09', 'HLA-DQA1*01:08-DQB1*03:10', 'HLA-DQA1*01:08-DQB1*03:11', 'HLA-DQA1*01:08-DQB1*03:12', 'HLA-DQA1*01:08-DQB1*03:13',
'HLA-DQA1*01:08-DQB1*03:14', 'HLA-DQA1*01:08-DQB1*03:15', 'HLA-DQA1*01:08-DQB1*03:16', 'HLA-DQA1*01:08-DQB1*03:17', 'HLA-DQA1*01:08-DQB1*03:18',
'HLA-DQA1*01:08-DQB1*03:19', 'HLA-DQA1*01:08-DQB1*03:20', 'HLA-DQA1*01:08-DQB1*03:21', 'HLA-DQA1*01:08-DQB1*03:22', 'HLA-DQA1*01:08-DQB1*03:23',
'HLA-DQA1*01:08-DQB1*03:24', 'HLA-DQA1*01:08-DQB1*03:25', 'HLA-DQA1*01:08-DQB1*03:26', 'HLA-DQA1*01:08-DQB1*03:27', 'HLA-DQA1*01:08-DQB1*03:28',
'HLA-DQA1*01:08-DQB1*03:29', 'HLA-DQA1*01:08-DQB1*03:30', 'HLA-DQA1*01:08-DQB1*03:31', 'HLA-DQA1*01:08-DQB1*03:32', 'HLA-DQA1*01:08-DQB1*03:33',
'HLA-DQA1*01:08-DQB1*03:34', 'HLA-DQA1*01:08-DQB1*03:35', 'HLA-DQA1*01:08-DQB1*03:36', 'HLA-DQA1*01:08-DQB1*03:37', 'HLA-DQA1*01:08-DQB1*03:38',
'HLA-DQA1*01:08-DQB1*04:01', 'HLA-DQA1*01:08-DQB1*04:02', 'HLA-DQA1*01:08-DQB1*04:03', 'HLA-DQA1*01:08-DQB1*04:04', 'HLA-DQA1*01:08-DQB1*04:05',
'HLA-DQA1*01:08-DQB1*04:06', 'HLA-DQA1*01:08-DQB1*04:07', 'HLA-DQA1*01:08-DQB1*04:08', 'HLA-DQA1*01:08-DQB1*05:01', 'HLA-DQA1*01:08-DQB1*05:02',
'HLA-DQA1*01:08-DQB1*05:03', 'HLA-DQA1*01:08-DQB1*05:05', 'HLA-DQA1*01:08-DQB1*05:06', 'HLA-DQA1*01:08-DQB1*05:07', 'HLA-DQA1*01:08-DQB1*05:08',
'HLA-DQA1*01:08-DQB1*05:09', 'HLA-DQA1*01:08-DQB1*05:10', 'HLA-DQA1*01:08-DQB1*05:11', 'HLA-DQA1*01:08-DQB1*05:12', 'HLA-DQA1*01:08-DQB1*05:13',
'HLA-DQA1*01:08-DQB1*05:14', 'HLA-DQA1*01:08-DQB1*06:01', 'HLA-DQA1*01:08-DQB1*06:02', 'HLA-DQA1*01:08-DQB1*06:03', 'HLA-DQA1*01:08-DQB1*06:04',
'HLA-DQA1*01:08-DQB1*06:07', 'HLA-DQA1*01:08-DQB1*06:08', 'HLA-DQA1*01:08-DQB1*06:09', 'HLA-DQA1*01:08-DQB1*06:10', 'HLA-DQA1*01:08-DQB1*06:11',
'HLA-DQA1*01:08-DQB1*06:12', 'HLA-DQA1*01:08-DQB1*06:14', 'HLA-DQA1*01:08-DQB1*06:15', 'HLA-DQA1*01:08-DQB1*06:16', 'HLA-DQA1*01:08-DQB1*06:17',
'HLA-DQA1*01:08-DQB1*06:18', 'HLA-DQA1*01:08-DQB1*06:19', 'HLA-DQA1*01:08-DQB1*06:21', 'HLA-DQA1*01:08-DQB1*06:22', 'HLA-DQA1*01:08-DQB1*06:23',
'HLA-DQA1*01:08-DQB1*06:24', 'HLA-DQA1*01:08-DQB1*06:25', 'HLA-DQA1*01:08-DQB1*06:27', 'HLA-DQA1*01:08-DQB1*06:28', 'HLA-DQA1*01:08-DQB1*06:29',
'HLA-DQA1*01:08-DQB1*06:30', 'HLA-DQA1*01:08-DQB1*06:31', 'HLA-DQA1*01:08-DQB1*06:32', 'HLA-DQA1*01:08-DQB1*06:33', 'HLA-DQA1*01:08-DQB1*06:34',
'HLA-DQA1*01:08-DQB1*06:35', 'HLA-DQA1*01:08-DQB1*06:36', 'HLA-DQA1*01:08-DQB1*06:37', 'HLA-DQA1*01:08-DQB1*06:38', 'HLA-DQA1*01:08-DQB1*06:39',
'HLA-DQA1*01:08-DQB1*06:40', 'HLA-DQA1*01:08-DQB1*06:41', 'HLA-DQA1*01:08-DQB1*06:42', 'HLA-DQA1*01:08-DQB1*06:43', 'HLA-DQA1*01:08-DQB1*06:44',
'HLA-DQA1*01:09-DQB1*02:01', 'HLA-DQA1*01:09-DQB1*02:02', 'HLA-DQA1*01:09-DQB1*02:03', 'HLA-DQA1*01:09-DQB1*02:04', 'HLA-DQA1*01:09-DQB1*02:05',
'HLA-DQA1*01:09-DQB1*02:06', 'HLA-DQA1*01:09-DQB1*03:01', 'HLA-DQA1*01:09-DQB1*03:02', 'HLA-DQA1*01:09-DQB1*03:03', 'HLA-DQA1*01:09-DQB1*03:04',
'HLA-DQA1*01:09-DQB1*03:05', 'HLA-DQA1*01:09-DQB1*03:06', 'HLA-DQA1*01:09-DQB1*03:07', 'HLA-DQA1*01:09-DQB1*03:08', 'HLA-DQA1*01:09-DQB1*03:09',
'HLA-DQA1*01:09-DQB1*03:10', 'HLA-DQA1*01:09-DQB1*03:11', 'HLA-DQA1*01:09-DQB1*03:12', 'HLA-DQA1*01:09-DQB1*03:13', 'HLA-DQA1*01:09-DQB1*03:14',
'HLA-DQA1*01:09-DQB1*03:15', 'HLA-DQA1*01:09-DQB1*03:16', 'HLA-DQA1*01:09-DQB1*03:17', 'HLA-DQA1*01:09-DQB1*03:18', 'HLA-DQA1*01:09-DQB1*03:19',
'HLA-DQA1*01:09-DQB1*03:20', 'HLA-DQA1*01:09-DQB1*03:21', 'HLA-DQA1*01:09-DQB1*03:22', 'HLA-DQA1*01:09-DQB1*03:23', 'HLA-DQA1*01:09-DQB1*03:24',
'HLA-DQA1*01:09-DQB1*03:25', 'HLA-DQA1*01:09-DQB1*03:26', 'HLA-DQA1*01:09-DQB1*03:27', 'HLA-DQA1*01:09-DQB1*03:28', 'HLA-DQA1*01:09-DQB1*03:29',
'HLA-DQA1*01:09-DQB1*03:30', 'HLA-DQA1*01:09-DQB1*03:31', 'HLA-DQA1*01:09-DQB1*03:32', 'HLA-DQA1*01:09-DQB1*03:33', 'HLA-DQA1*01:09-DQB1*03:34',
'HLA-DQA1*01:09-DQB1*03:35', 'HLA-DQA1*01:09-DQB1*03:36', 'HLA-DQA1*01:09-DQB1*03:37', 'HLA-DQA1*01:09-DQB1*03:38', 'HLA-DQA1*01:09-DQB1*04:01',
'HLA-DQA1*01:09-DQB1*04:02', 'HLA-DQA1*01:09-DQB1*04:03', 'HLA-DQA1*01:09-DQB1*04:04', 'HLA-DQA1*01:09-DQB1*04:05', 'HLA-DQA1*01:09-DQB1*04:06',
'HLA-DQA1*01:09-DQB1*04:07', 'HLA-DQA1*01:09-DQB1*04:08', 'HLA-DQA1*01:09-DQB1*05:01', 'HLA-DQA1*01:09-DQB1*05:02', 'HLA-DQA1*01:09-DQB1*05:03',
'HLA-DQA1*01:09-DQB1*05:05', 'HLA-DQA1*01:09-DQB1*05:06', 'HLA-DQA1*01:09-DQB1*05:07', 'HLA-DQA1*01:09-DQB1*05:08', 'HLA-DQA1*01:09-DQB1*05:09',
'HLA-DQA1*01:09-DQB1*05:10', 'HLA-DQA1*01:09-DQB1*05:11', 'HLA-DQA1*01:09-DQB1*05:12', 'HLA-DQA1*01:09-DQB1*05:13', 'HLA-DQA1*01:09-DQB1*05:14',
'HLA-DQA1*01:09-DQB1*06:01', 'HLA-DQA1*01:09-DQB1*06:02', 'HLA-DQA1*01:09-DQB1*06:03', 'HLA-DQA1*01:09-DQB1*06:04', 'HLA-DQA1*01:09-DQB1*06:07',
'HLA-DQA1*01:09-DQB1*06:08', 'HLA-DQA1*01:09-DQB1*06:09', 'HLA-DQA1*01:09-DQB1*06:10', 'HLA-DQA1*01:09-DQB1*06:11', 'HLA-DQA1*01:09-DQB1*06:12',
'HLA-DQA1*01:09-DQB1*06:14', 'HLA-DQA1*01:09-DQB1*06:15', 'HLA-DQA1*01:09-DQB1*06:16', 'HLA-DQA1*01:09-DQB1*06:17', 'HLA-DQA1*01:09-DQB1*06:18',
'HLA-DQA1*01:09-DQB1*06:19', 'HLA-DQA1*01:09-DQB1*06:21', 'HLA-DQA1*01:09-DQB1*06:22', 'HLA-DQA1*01:09-DQB1*06:23', 'HLA-DQA1*01:09-DQB1*06:24',
'HLA-DQA1*01:09-DQB1*06:25', 'HLA-DQA1*01:09-DQB1*06:27', 'HLA-DQA1*01:09-DQB1*06:28', 'HLA-DQA1*01:09-DQB1*06:29', 'HLA-DQA1*01:09-DQB1*06:30',
'HLA-DQA1*01:09-DQB1*06:31', 'HLA-DQA1*01:09-DQB1*06:32', 'HLA-DQA1*01:09-DQB1*06:33', 'HLA-DQA1*01:09-DQB1*06:34', 'HLA-DQA1*01:09-DQB1*06:35',
'HLA-DQA1*01:09-DQB1*06:36', 'HLA-DQA1*01:09-DQB1*06:37', 'HLA-DQA1*01:09-DQB1*06:38', 'HLA-DQA1*01:09-DQB1*06:39', 'HLA-DQA1*01:09-DQB1*06:40',
'HLA-DQA1*01:09-DQB1*06:41', 'HLA-DQA1*01:09-DQB1*06:42', 'HLA-DQA1*01:09-DQB1*06:43', 'HLA-DQA1*01:09-DQB1*06:44', 'HLA-DQA1*02:01-DQB1*02:01',
'HLA-DQA1*02:01-DQB1*02:02', 'HLA-DQA1*02:01-DQB1*02:03', 'HLA-DQA1*02:01-DQB1*02:04', 'HLA-DQA1*02:01-DQB1*02:05', 'HLA-DQA1*02:01-DQB1*02:06',
'HLA-DQA1*02:01-DQB1*03:01', 'HLA-DQA1*02:01-DQB1*03:02', 'HLA-DQA1*02:01-DQB1*03:03', 'HLA-DQA1*02:01-DQB1*03:04', 'HLA-DQA1*02:01-DQB1*03:05',
'HLA-DQA1*02:01-DQB1*03:06', 'HLA-DQA1*02:01-DQB1*03:07', 'HLA-DQA1*02:01-DQB1*03:08', 'HLA-DQA1*02:01-DQB1*03:09', 'HLA-DQA1*02:01-DQB1*03:10',
'HLA-DQA1*02:01-DQB1*03:11', 'HLA-DQA1*02:01-DQB1*03:12', 'HLA-DQA1*02:01-DQB1*03:13', 'HLA-DQA1*02:01-DQB1*03:14', 'HLA-DQA1*02:01-DQB1*03:15',
'HLA-DQA1*02:01-DQB1*03:16', 'HLA-DQA1*02:01-DQB1*03:17', 'HLA-DQA1*02:01-DQB1*03:18', 'HLA-DQA1*02:01-DQB1*03:19', 'HLA-DQA1*02:01-DQB1*03:20',
'HLA-DQA1*02:01-DQB1*03:21', 'HLA-DQA1*02:01-DQB1*03:22', 'HLA-DQA1*02:01-DQB1*03:23', 'HLA-DQA1*02:01-DQB1*03:24', 'HLA-DQA1*02:01-DQB1*03:25',
'HLA-DQA1*02:01-DQB1*03:26', 'HLA-DQA1*02:01-DQB1*03:27', 'HLA-DQA1*02:01-DQB1*03:28', 'HLA-DQA1*02:01-DQB1*03:29', 'HLA-DQA1*02:01-DQB1*03:30',
'HLA-DQA1*02:01-DQB1*03:31', 'HLA-DQA1*02:01-DQB1*03:32', 'HLA-DQA1*02:01-DQB1*03:33', 'HLA-DQA1*02:01-DQB1*03:34', 'HLA-DQA1*02:01-DQB1*03:35',
'HLA-DQA1*02:01-DQB1*03:36', 'HLA-DQA1*02:01-DQB1*03:37', 'HLA-DQA1*02:01-DQB1*03:38', 'HLA-DQA1*02:01-DQB1*04:01', 'HLA-DQA1*02:01-DQB1*04:02',
'HLA-DQA1*02:01-DQB1*04:03', 'HLA-DQA1*02:01-DQB1*04:04', 'HLA-DQA1*02:01-DQB1*04:05', 'HLA-DQA1*02:01-DQB1*04:06', 'HLA-DQA1*02:01-DQB1*04:07',
'HLA-DQA1*02:01-DQB1*04:08', 'HLA-DQA1*02:01-DQB1*05:01', 'HLA-DQA1*02:01-DQB1*05:02', 'HLA-DQA1*02:01-DQB1*05:03', 'HLA-DQA1*02:01-DQB1*05:05',
'HLA-DQA1*02:01-DQB1*05:06', 'HLA-DQA1*02:01-DQB1*05:07', 'HLA-DQA1*02:01-DQB1*05:08', 'HLA-DQA1*02:01-DQB1*05:09', 'HLA-DQA1*02:01-DQB1*05:10',
'HLA-DQA1*02:01-DQB1*05:11', 'HLA-DQA1*02:01-DQB1*05:12', 'HLA-DQA1*02:01-DQB1*05:13', 'HLA-DQA1*02:01-DQB1*05:14', 'HLA-DQA1*02:01-DQB1*06:01',
'HLA-DQA1*02:01-DQB1*06:02', 'HLA-DQA1*02:01-DQB1*06:03', 'HLA-DQA1*02:01-DQB1*06:04', 'HLA-DQA1*02:01-DQB1*06:07', 'HLA-DQA1*02:01-DQB1*06:08',
'HLA-DQA1*02:01-DQB1*06:09', 'HLA-DQA1*02:01-DQB1*06:10', 'HLA-DQA1*02:01-DQB1*06:11', 'HLA-DQA1*02:01-DQB1*06:12', 'HLA-DQA1*02:01-DQB1*06:14',
'HLA-DQA1*02:01-DQB1*06:15', 'HLA-DQA1*02:01-DQB1*06:16', 'HLA-DQA1*02:01-DQB1*06:17', 'HLA-DQA1*02:01-DQB1*06:18', 'HLA-DQA1*02:01-DQB1*06:19',
'HLA-DQA1*02:01-DQB1*06:21', 'HLA-DQA1*02:01-DQB1*06:22', 'HLA-DQA1*02:01-DQB1*06:23', 'HLA-DQA1*02:01-DQB1*06:24', 'HLA-DQA1*02:01-DQB1*06:25',
'HLA-DQA1*02:01-DQB1*06:27', 'HLA-DQA1*02:01-DQB1*06:28', 'HLA-DQA1*02:01-DQB1*06:29', 'HLA-DQA1*02:01-DQB1*06:30', 'HLA-DQA1*02:01-DQB1*06:31',
'HLA-DQA1*02:01-DQB1*06:32', 'HLA-DQA1*02:01-DQB1*06:33', 'HLA-DQA1*02:01-DQB1*06:34', 'HLA-DQA1*02:01-DQB1*06:35', 'HLA-DQA1*02:01-DQB1*06:36',
'HLA-DQA1*02:01-DQB1*06:37', 'HLA-DQA1*02:01-DQB1*06:38', 'HLA-DQA1*02:01-DQB1*06:39', 'HLA-DQA1*02:01-DQB1*06:40', 'HLA-DQA1*02:01-DQB1*06:41',
'HLA-DQA1*02:01-DQB1*06:42', 'HLA-DQA1*02:01-DQB1*06:43', 'HLA-DQA1*02:01-DQB1*06:44', 'HLA-DQA1*03:01-DQB1*02:01', 'HLA-DQA1*03:01-DQB1*02:02',
'HLA-DQA1*03:01-DQB1*02:03', 'HLA-DQA1*03:01-DQB1*02:04', 'HLA-DQA1*03:01-DQB1*02:05', 'HLA-DQA1*03:01-DQB1*02:06', 'HLA-DQA1*03:01-DQB1*03:01',
'HLA-DQA1*03:01-DQB1*03:02', 'HLA-DQA1*03:01-DQB1*03:03', 'HLA-DQA1*03:01-DQB1*03:04', 'HLA-DQA1*03:01-DQB1*03:05', 'HLA-DQA1*03:01-DQB1*03:06',
'HLA-DQA1*03:01-DQB1*03:07', 'HLA-DQA1*03:01-DQB1*03:08', 'HLA-DQA1*03:01-DQB1*03:09', 'HLA-DQA1*03:01-DQB1*03:10', 'HLA-DQA1*03:01-DQB1*03:11',
'HLA-DQA1*03:01-DQB1*03:12', 'HLA-DQA1*03:01-DQB1*03:13', 'HLA-DQA1*03:01-DQB1*03:14', 'HLA-DQA1*03:01-DQB1*03:15', 'HLA-DQA1*03:01-DQB1*03:16',
'HLA-DQA1*03:01-DQB1*03:17', 'HLA-DQA1*03:01-DQB1*03:18', 'HLA-DQA1*03:01-DQB1*03:19', 'HLA-DQA1*03:01-DQB1*03:20', 'HLA-DQA1*03:01-DQB1*03:21',
'HLA-DQA1*03:01-DQB1*03:22', 'HLA-DQA1*03:01-DQB1*03:23', 'HLA-DQA1*03:01-DQB1*03:24', 'HLA-DQA1*03:01-DQB1*03:25', 'HLA-DQA1*03:01-DQB1*03:26',
'HLA-DQA1*03:01-DQB1*03:27', 'HLA-DQA1*03:01-DQB1*03:28', 'HLA-DQA1*03:01-DQB1*03:29', 'HLA-DQA1*03:01-DQB1*03:30', 'HLA-DQA1*03:01-DQB1*03:31',
'HLA-DQA1*03:01-DQB1*03:32', 'HLA-DQA1*03:01-DQB1*03:33', 'HLA-DQA1*03:01-DQB1*03:34', 'HLA-DQA1*03:01-DQB1*03:35', 'HLA-DQA1*03:01-DQB1*03:36',
'HLA-DQA1*03:01-DQB1*03:37', 'HLA-DQA1*03:01-DQB1*03:38', 'HLA-DQA1*03:01-DQB1*04:01', 'HLA-DQA1*03:01-DQB1*04:02', 'HLA-DQA1*03:01-DQB1*04:03',
'HLA-DQA1*03:01-DQB1*04:04', 'HLA-DQA1*03:01-DQB1*04:05', 'HLA-DQA1*03:01-DQB1*04:06', 'HLA-DQA1*03:01-DQB1*04:07', 'HLA-DQA1*03:01-DQB1*04:08',
'HLA-DQA1*03:01-DQB1*05:01', 'HLA-DQA1*03:01-DQB1*05:02', 'HLA-DQA1*03:01-DQB1*05:03', 'HLA-DQA1*03:01-DQB1*05:05', 'HLA-DQA1*03:01-DQB1*05:06',
'HLA-DQA1*03:01-DQB1*05:07', 'HLA-DQA1*03:01-DQB1*05:08', 'HLA-DQA1*03:01-DQB1*05:09', 'HLA-DQA1*03:01-DQB1*05:10', 'HLA-DQA1*03:01-DQB1*05:11',
'HLA-DQA1*03:01-DQB1*05:12', 'HLA-DQA1*03:01-DQB1*05:13', 'HLA-DQA1*03:01-DQB1*05:14', 'HLA-DQA1*03:01-DQB1*06:01', 'HLA-DQA1*03:01-DQB1*06:02',
'HLA-DQA1*03:01-DQB1*06:03', 'HLA-DQA1*03:01-DQB1*06:04', 'HLA-DQA1*03:01-DQB1*06:07', 'HLA-DQA1*03:01-DQB1*06:08', 'HLA-DQA1*03:01-DQB1*06:09',
'HLA-DQA1*03:01-DQB1*06:10', 'HLA-DQA1*03:01-DQB1*06:11', 'HLA-DQA1*03:01-DQB1*06:12', 'HLA-DQA1*03:01-DQB1*06:14', 'HLA-DQA1*03:01-DQB1*06:15',
'HLA-DQA1*03:01-DQB1*06:16', 'HLA-DQA1*03:01-DQB1*06:17', 'HLA-DQA1*03:01-DQB1*06:18', 'HLA-DQA1*03:01-DQB1*06:19', 'HLA-DQA1*03:01-DQB1*06:21',
'HLA-DQA1*03:01-DQB1*06:22', 'HLA-DQA1*03:01-DQB1*06:23', 'HLA-DQA1*03:01-DQB1*06:24', 'HLA-DQA1*03:01-DQB1*06:25', 'HLA-DQA1*03:01-DQB1*06:27',
'HLA-DQA1*03:01-DQB1*06:28', 'HLA-DQA1*03:01-DQB1*06:29', 'HLA-DQA1*03:01-DQB1*06:30', 'HLA-DQA1*03:01-DQB1*06:31', 'HLA-DQA1*03:01-DQB1*06:32',
'HLA-DQA1*03:01-DQB1*06:33', 'HLA-DQA1*03:01-DQB1*06:34', 'HLA-DQA1*03:01-DQB1*06:35', 'HLA-DQA1*03:01-DQB1*06:36', 'HLA-DQA1*03:01-DQB1*06:37',
'HLA-DQA1*03:01-DQB1*06:38', 'HLA-DQA1*03:01-DQB1*06:39', 'HLA-DQA1*03:01-DQB1*06:40', 'HLA-DQA1*03:01-DQB1*06:41', 'HLA-DQA1*03:01-DQB1*06:42',
'HLA-DQA1*03:01-DQB1*06:43', 'HLA-DQA1*03:01-DQB1*06:44', 'HLA-DQA1*03:02-DQB1*02:01', 'HLA-DQA1*03:02-DQB1*02:02', 'HLA-DQA1*03:02-DQB1*02:03',
'HLA-DQA1*03:02-DQB1*02:04', 'HLA-DQA1*03:02-DQB1*02:05', 'HLA-DQA1*03:02-DQB1*02:06', 'HLA-DQA1*03:02-DQB1*03:01', 'HLA-DQA1*03:02-DQB1*03:02',
'HLA-DQA1*03:02-DQB1*03:03', 'HLA-DQA1*03:02-DQB1*03:04', 'HLA-DQA1*03:02-DQB1*03:05', 'HLA-DQA1*03:02-DQB1*03:06', 'HLA-DQA1*03:02-DQB1*03:07',
'HLA-DQA1*03:02-DQB1*03:08', 'HLA-DQA1*03:02-DQB1*03:09', 'HLA-DQA1*03:02-DQB1*03:10', 'HLA-DQA1*03:02-DQB1*03:11', 'HLA-DQA1*03:02-DQB1*03:12',
'HLA-DQA1*03:02-DQB1*03:13', 'HLA-DQA1*03:02-DQB1*03:14', 'HLA-DQA1*03:02-DQB1*03:15', 'HLA-DQA1*03:02-DQB1*03:16', 'HLA-DQA1*03:02-DQB1*03:17',
'HLA-DQA1*03:02-DQB1*03:18', 'HLA-DQA1*03:02-DQB1*03:19', 'HLA-DQA1*03:02-DQB1*03:20', 'HLA-DQA1*03:02-DQB1*03:21', 'HLA-DQA1*03:02-DQB1*03:22',
'HLA-DQA1*03:02-DQB1*03:23', 'HLA-DQA1*03:02-DQB1*03:24', 'HLA-DQA1*03:02-DQB1*03:25', 'HLA-DQA1*03:02-DQB1*03:26', 'HLA-DQA1*03:02-DQB1*03:27',
'HLA-DQA1*03:02-DQB1*03:28', 'HLA-DQA1*03:02-DQB1*03:29', 'HLA-DQA1*03:02-DQB1*03:30', 'HLA-DQA1*03:02-DQB1*03:31', 'HLA-DQA1*03:02-DQB1*03:32',
'HLA-DQA1*03:02-DQB1*03:33', 'HLA-DQA1*03:02-DQB1*03:34', 'HLA-DQA1*03:02-DQB1*03:35', 'HLA-DQA1*03:02-DQB1*03:36', 'HLA-DQA1*03:02-DQB1*03:37',
'HLA-DQA1*03:02-DQB1*03:38', 'HLA-DQA1*03:02-DQB1*04:01', 'HLA-DQA1*03:02-DQB1*04:02', 'HLA-DQA1*03:02-DQB1*04:03', 'HLA-DQA1*03:02-DQB1*04:04',
'HLA-DQA1*03:02-DQB1*04:05', 'HLA-DQA1*03:02-DQB1*04:06', 'HLA-DQA1*03:02-DQB1*04:07', 'HLA-DQA1*03:02-DQB1*04:08', 'HLA-DQA1*03:02-DQB1*05:01',
'HLA-DQA1*03:02-DQB1*05:02', 'HLA-DQA1*03:02-DQB1*05:03', 'HLA-DQA1*03:02-DQB1*05:05', 'HLA-DQA1*03:02-DQB1*05:06', 'HLA-DQA1*03:02-DQB1*05:07',
'HLA-DQA1*03:02-DQB1*05:08', 'HLA-DQA1*03:02-DQB1*05:09', 'HLA-DQA1*03:02-DQB1*05:10', 'HLA-DQA1*03:02-DQB1*05:11', 'HLA-DQA1*03:02-DQB1*05:12',
'HLA-DQA1*03:02-DQB1*05:13', 'HLA-DQA1*03:02-DQB1*05:14', 'HLA-DQA1*03:02-DQB1*06:01', 'HLA-DQA1*03:02-DQB1*06:02', 'HLA-DQA1*03:02-DQB1*06:03',
'HLA-DQA1*03:02-DQB1*06:04', 'HLA-DQA1*03:02-DQB1*06:07', 'HLA-DQA1*03:02-DQB1*06:08', 'HLA-DQA1*03:02-DQB1*06:09', 'HLA-DQA1*03:02-DQB1*06:10',
'HLA-DQA1*03:02-DQB1*06:11', 'HLA-DQA1*03:02-DQB1*06:12', 'HLA-DQA1*03:02-DQB1*06:14', 'HLA-DQA1*03:02-DQB1*06:15', 'HLA-DQA1*03:02-DQB1*06:16',
'HLA-DQA1*03:02-DQB1*06:17', 'HLA-DQA1*03:02-DQB1*06:18', 'HLA-DQA1*03:02-DQB1*06:19', 'HLA-DQA1*03:02-DQB1*06:21', 'HLA-DQA1*03:02-DQB1*06:22',
'HLA-DQA1*03:02-DQB1*06:23', 'HLA-DQA1*03:02-DQB1*06:24', 'HLA-DQA1*03:02-DQB1*06:25', 'HLA-DQA1*03:02-DQB1*06:27', 'HLA-DQA1*03:02-DQB1*06:28',
'HLA-DQA1*03:02-DQB1*06:29', 'HLA-DQA1*03:02-DQB1*06:30', 'HLA-DQA1*03:02-DQB1*06:31', 'HLA-DQA1*03:02-DQB1*06:32', 'HLA-DQA1*03:02-DQB1*06:33',
'HLA-DQA1*03:02-DQB1*06:34', 'HLA-DQA1*03:02-DQB1*06:35', 'HLA-DQA1*03:02-DQB1*06:36', 'HLA-DQA1*03:02-DQB1*06:37', 'HLA-DQA1*03:02-DQB1*06:38',
'HLA-DQA1*03:02-DQB1*06:39', 'HLA-DQA1*03:02-DQB1*06:40', 'HLA-DQA1*03:02-DQB1*06:41', 'HLA-DQA1*03:02-DQB1*06:42', 'HLA-DQA1*03:02-DQB1*06:43',
'HLA-DQA1*03:02-DQB1*06:44', 'HLA-DQA1*03:03-DQB1*02:01', 'HLA-DQA1*03:03-DQB1*02:02', 'HLA-DQA1*03:03-DQB1*02:03', 'HLA-DQA1*03:03-DQB1*02:04',
'HLA-DQA1*03:03-DQB1*02:05', 'HLA-DQA1*03:03-DQB1*02:06', 'HLA-DQA1*03:03-DQB1*03:01', 'HLA-DQA1*03:03-DQB1*03:02', 'HLA-DQA1*03:03-DQB1*03:03',
'HLA-DQA1*03:03-DQB1*03:04', 'HLA-DQA1*03:03-DQB1*03:05', 'HLA-DQA1*03:03-DQB1*03:06', 'HLA-DQA1*03:03-DQB1*03:07', 'HLA-DQA1*03:03-DQB1*03:08',
'HLA-DQA1*03:03-DQB1*03:09', 'HLA-DQA1*03:03-DQB1*03:10', 'HLA-DQA1*03:03-DQB1*03:11', 'HLA-DQA1*03:03-DQB1*03:12', 'HLA-DQA1*03:03-DQB1*03:13',
'HLA-DQA1*03:03-DQB1*03:14', 'HLA-DQA1*03:03-DQB1*03:15', 'HLA-DQA1*03:03-DQB1*03:16', 'HLA-DQA1*03:03-DQB1*03:17', 'HLA-DQA1*03:03-DQB1*03:18',
'HLA-DQA1*03:03-DQB1*03:19', 'HLA-DQA1*03:03-DQB1*03:20', 'HLA-DQA1*03:03-DQB1*03:21', 'HLA-DQA1*03:03-DQB1*03:22', 'HLA-DQA1*03:03-DQB1*03:23',
'HLA-DQA1*03:03-DQB1*03:24', 'HLA-DQA1*03:03-DQB1*03:25', 'HLA-DQA1*03:03-DQB1*03:26', 'HLA-DQA1*03:03-DQB1*03:27', 'HLA-DQA1*03:03-DQB1*03:28',
'HLA-DQA1*03:03-DQB1*03:29', 'HLA-DQA1*03:03-DQB1*03:30', 'HLA-DQA1*03:03-DQB1*03:31', 'HLA-DQA1*03:03-DQB1*03:32', 'HLA-DQA1*03:03-DQB1*03:33',
'HLA-DQA1*03:03-DQB1*03:34', 'HLA-DQA1*03:03-DQB1*03:35', 'HLA-DQA1*03:03-DQB1*03:36', 'HLA-DQA1*03:03-DQB1*03:37', 'HLA-DQA1*03:03-DQB1*03:38',
'HLA-DQA1*03:03-DQB1*04:01', 'HLA-DQA1*03:03-DQB1*04:02', 'HLA-DQA1*03:03-DQB1*04:03', 'HLA-DQA1*03:03-DQB1*04:04', 'HLA-DQA1*03:03-DQB1*04:05',
'HLA-DQA1*03:03-DQB1*04:06', 'HLA-DQA1*03:03-DQB1*04:07', 'HLA-DQA1*03:03-DQB1*04:08', 'HLA-DQA1*03:03-DQB1*05:01', 'HLA-DQA1*03:03-DQB1*05:02',
'HLA-DQA1*03:03-DQB1*05:03', 'HLA-DQA1*03:03-DQB1*05:05', 'HLA-DQA1*03:03-DQB1*05:06', 'HLA-DQA1*03:03-DQB1*05:07', 'HLA-DQA1*03:03-DQB1*05:08',
'HLA-DQA1*03:03-DQB1*05:09', 'HLA-DQA1*03:03-DQB1*05:10', 'HLA-DQA1*03:03-DQB1*05:11', 'HLA-DQA1*03:03-DQB1*05:12', 'HLA-DQA1*03:03-DQB1*05:13',
'HLA-DQA1*03:03-DQB1*05:14', 'HLA-DQA1*03:03-DQB1*06:01', 'HLA-DQA1*03:03-DQB1*06:02', 'HLA-DQA1*03:03-DQB1*06:03', 'HLA-DQA1*03:03-DQB1*06:04',
'HLA-DQA1*03:03-DQB1*06:07', 'HLA-DQA1*03:03-DQB1*06:08', 'HLA-DQA1*03:03-DQB1*06:09', 'HLA-DQA1*03:03-DQB1*06:10', 'HLA-DQA1*03:03-DQB1*06:11',
'HLA-DQA1*03:03-DQB1*06:12', 'HLA-DQA1*03:03-DQB1*06:14', 'HLA-DQA1*03:03-DQB1*06:15', 'HLA-DQA1*03:03-DQB1*06:16', 'HLA-DQA1*03:03-DQB1*06:17',
'HLA-DQA1*03:03-DQB1*06:18', 'HLA-DQA1*03:03-DQB1*06:19', 'HLA-DQA1*03:03-DQB1*06:21', 'HLA-DQA1*03:03-DQB1*06:22', 'HLA-DQA1*03:03-DQB1*06:23',
'HLA-DQA1*03:03-DQB1*06:24', 'HLA-DQA1*03:03-DQB1*06:25', 'HLA-DQA1*03:03-DQB1*06:27', 'HLA-DQA1*03:03-DQB1*06:28', 'HLA-DQA1*03:03-DQB1*06:29',
'HLA-DQA1*03:03-DQB1*06:30', 'HLA-DQA1*03:03-DQB1*06:31', 'HLA-DQA1*03:03-DQB1*06:32', 'HLA-DQA1*03:03-DQB1*06:33', 'HLA-DQA1*03:03-DQB1*06:34',
'HLA-DQA1*03:03-DQB1*06:35', 'HLA-DQA1*03:03-DQB1*06:36', 'HLA-DQA1*03:03-DQB1*06:37', 'HLA-DQA1*03:03-DQB1*06:38', 'HLA-DQA1*03:03-DQB1*06:39',
'HLA-DQA1*03:03-DQB1*06:40', 'HLA-DQA1*03:03-DQB1*06:41', 'HLA-DQA1*03:03-DQB1*06:42', 'HLA-DQA1*03:03-DQB1*06:43', 'HLA-DQA1*03:03-DQB1*06:44',
'HLA-DQA1*04:01-DQB1*02:01', 'HLA-DQA1*04:01-DQB1*02:02', 'HLA-DQA1*04:01-DQB1*02:03', 'HLA-DQA1*04:01-DQB1*02:04', 'HLA-DQA1*04:01-DQB1*02:05',
'HLA-DQA1*04:01-DQB1*02:06', 'HLA-DQA1*04:01-DQB1*03:01', 'HLA-DQA1*04:01-DQB1*03:02', 'HLA-DQA1*04:01-DQB1*03:03', 'HLA-DQA1*04:01-DQB1*03:04',
'HLA-DQA1*04:01-DQB1*03:05', 'HLA-DQA1*04:01-DQB1*03:06', 'HLA-DQA1*04:01-DQB1*03:07', 'HLA-DQA1*04:01-DQB1*03:08', 'HLA-DQA1*04:01-DQB1*03:09',
'HLA-DQA1*04:01-DQB1*03:10', 'HLA-DQA1*04:01-DQB1*03:11', 'HLA-DQA1*04:01-DQB1*03:12', 'HLA-DQA1*04:01-DQB1*03:13', 'HLA-DQA1*04:01-DQB1*03:14',
'HLA-DQA1*04:01-DQB1*03:15', 'HLA-DQA1*04:01-DQB1*03:16', 'HLA-DQA1*04:01-DQB1*03:17', 'HLA-DQA1*04:01-DQB1*03:18', 'HLA-DQA1*04:01-DQB1*03:19',
'HLA-DQA1*04:01-DQB1*03:20', 'HLA-DQA1*04:01-DQB1*03:21', 'HLA-DQA1*04:01-DQB1*03:22', 'HLA-DQA1*04:01-DQB1*03:23', 'HLA-DQA1*04:01-DQB1*03:24',
'HLA-DQA1*04:01-DQB1*03:25', 'HLA-DQA1*04:01-DQB1*03:26', 'HLA-DQA1*04:01-DQB1*03:27', 'HLA-DQA1*04:01-DQB1*03:28', 'HLA-DQA1*04:01-DQB1*03:29',
'HLA-DQA1*04:01-DQB1*03:30', 'HLA-DQA1*04:01-DQB1*03:31', 'HLA-DQA1*04:01-DQB1*03:32', 'HLA-DQA1*04:01-DQB1*03:33', 'HLA-DQA1*04:01-DQB1*03:34',
'HLA-DQA1*04:01-DQB1*03:35', 'HLA-DQA1*04:01-DQB1*03:36', 'HLA-DQA1*04:01-DQB1*03:37', 'HLA-DQA1*04:01-DQB1*03:38', 'HLA-DQA1*04:01-DQB1*04:01',
'HLA-DQA1*04:01-DQB1*04:02', 'HLA-DQA1*04:01-DQB1*04:03', 'HLA-DQA1*04:01-DQB1*04:04', 'HLA-DQA1*04:01-DQB1*04:05', 'HLA-DQA1*04:01-DQB1*04:06',
'HLA-DQA1*04:01-DQB1*04:07', 'HLA-DQA1*04:01-DQB1*04:08', 'HLA-DQA1*04:01-DQB1*05:01', 'HLA-DQA1*04:01-DQB1*05:02', 'HLA-DQA1*04:01-DQB1*05:03',
'HLA-DQA1*04:01-DQB1*05:05', 'HLA-DQA1*04:01-DQB1*05:06', 'HLA-DQA1*04:01-DQB1*05:07', 'HLA-DQA1*04:01-DQB1*05:08', 'HLA-DQA1*04:01-DQB1*05:09',
'HLA-DQA1*04:01-DQB1*05:10', 'HLA-DQA1*04:01-DQB1*05:11', 'HLA-DQA1*04:01-DQB1*05:12', 'HLA-DQA1*04:01-DQB1*05:13', 'HLA-DQA1*04:01-DQB1*05:14',
'HLA-DQA1*04:01-DQB1*06:01', 'HLA-DQA1*04:01-DQB1*06:02', 'HLA-DQA1*04:01-DQB1*06:03', 'HLA-DQA1*04:01-DQB1*06:04', 'HLA-DQA1*04:01-DQB1*06:07',
'HLA-DQA1*04:01-DQB1*06:08', 'HLA-DQA1*04:01-DQB1*06:09', 'HLA-DQA1*04:01-DQB1*06:10', 'HLA-DQA1*04:01-DQB1*06:11', 'HLA-DQA1*04:01-DQB1*06:12',
'HLA-DQA1*04:01-DQB1*06:14', 'HLA-DQA1*04:01-DQB1*06:15', 'HLA-DQA1*04:01-DQB1*06:16', 'HLA-DQA1*04:01-DQB1*06:17', 'HLA-DQA1*04:01-DQB1*06:18',
'HLA-DQA1*04:01-DQB1*06:19', 'HLA-DQA1*04:01-DQB1*06:21', 'HLA-DQA1*04:01-DQB1*06:22', 'HLA-DQA1*04:01-DQB1*06:23', 'HLA-DQA1*04:01-DQB1*06:24',
'HLA-DQA1*04:01-DQB1*06:25', 'HLA-DQA1*04:01-DQB1*06:27', 'HLA-DQA1*04:01-DQB1*06:28', 'HLA-DQA1*04:01-DQB1*06:29', 'HLA-DQA1*04:01-DQB1*06:30',
'HLA-DQA1*04:01-DQB1*06:31', 'HLA-DQA1*04:01-DQB1*06:32', 'HLA-DQA1*04:01-DQB1*06:33', 'HLA-DQA1*04:01-DQB1*06:34', 'HLA-DQA1*04:01-DQB1*06:35',
'HLA-DQA1*04:01-DQB1*06:36', 'HLA-DQA1*04:01-DQB1*06:37', 'HLA-DQA1*04:01-DQB1*06:38', 'HLA-DQA1*04:01-DQB1*06:39', 'HLA-DQA1*04:01-DQB1*06:40',
'HLA-DQA1*04:01-DQB1*06:41', 'HLA-DQA1*04:01-DQB1*06:42', 'HLA-DQA1*04:01-DQB1*06:43', 'HLA-DQA1*04:01-DQB1*06:44', 'HLA-DQA1*04:02-DQB1*02:01',
'HLA-DQA1*04:02-DQB1*02:02', 'HLA-DQA1*04:02-DQB1*02:03', 'HLA-DQA1*04:02-DQB1*02:04', 'HLA-DQA1*04:02-DQB1*02:05', 'HLA-DQA1*04:02-DQB1*02:06',
'HLA-DQA1*04:02-DQB1*03:01', 'HLA-DQA1*04:02-DQB1*03:02', 'HLA-DQA1*04:02-DQB1*03:03', 'HLA-DQA1*04:02-DQB1*03:04', 'HLA-DQA1*04:02-DQB1*03:05',
'HLA-DQA1*04:02-DQB1*03:06', 'HLA-DQA1*04:02-DQB1*03:07', 'HLA-DQA1*04:02-DQB1*03:08', 'HLA-DQA1*04:02-DQB1*03:09', 'HLA-DQA1*04:02-DQB1*03:10',
'HLA-DQA1*04:02-DQB1*03:11', 'HLA-DQA1*04:02-DQB1*03:12', 'HLA-DQA1*04:02-DQB1*03:13', 'HLA-DQA1*04:02-DQB1*03:14', 'HLA-DQA1*04:02-DQB1*03:15',
'HLA-DQA1*04:02-DQB1*03:16', 'HLA-DQA1*04:02-DQB1*03:17', 'HLA-DQA1*04:02-DQB1*03:18', 'HLA-DQA1*04:02-DQB1*03:19', 'HLA-DQA1*04:02-DQB1*03:20',
'HLA-DQA1*04:02-DQB1*03:21', 'HLA-DQA1*04:02-DQB1*03:22', 'HLA-DQA1*04:02-DQB1*03:23', 'HLA-DQA1*04:02-DQB1*03:24', 'HLA-DQA1*04:02-DQB1*03:25',
'HLA-DQA1*04:02-DQB1*03:26', 'HLA-DQA1*04:02-DQB1*03:27', 'HLA-DQA1*04:02-DQB1*03:28', 'HLA-DQA1*04:02-DQB1*03:29', 'HLA-DQA1*04:02-DQB1*03:30',
'HLA-DQA1*04:02-DQB1*03:31', 'HLA-DQA1*04:02-DQB1*03:32', 'HLA-DQA1*04:02-DQB1*03:33', 'HLA-DQA1*04:02-DQB1*03:34', 'HLA-DQA1*04:02-DQB1*03:35',
'HLA-DQA1*04:02-DQB1*03:36', 'HLA-DQA1*04:02-DQB1*03:37', 'HLA-DQA1*04:02-DQB1*03:38', 'HLA-DQA1*04:02-DQB1*04:01', 'HLA-DQA1*04:02-DQB1*04:02',
'HLA-DQA1*04:02-DQB1*04:03', 'HLA-DQA1*04:02-DQB1*04:04', 'HLA-DQA1*04:02-DQB1*04:05', 'HLA-DQA1*04:02-DQB1*04:06', 'HLA-DQA1*04:02-DQB1*04:07',
'HLA-DQA1*04:02-DQB1*04:08', 'HLA-DQA1*04:02-DQB1*05:01', 'HLA-DQA1*04:02-DQB1*05:02', 'HLA-DQA1*04:02-DQB1*05:03', 'HLA-DQA1*04:02-DQB1*05:05',
'HLA-DQA1*04:02-DQB1*05:06', 'HLA-DQA1*04:02-DQB1*05:07', 'HLA-DQA1*04:02-DQB1*05:08', 'HLA-DQA1*04:02-DQB1*05:09', 'HLA-DQA1*04:02-DQB1*05:10',
'HLA-DQA1*04:02-DQB1*05:11', 'HLA-DQA1*04:02-DQB1*05:12', 'HLA-DQA1*04:02-DQB1*05:13', 'HLA-DQA1*04:02-DQB1*05:14', 'HLA-DQA1*04:02-DQB1*06:01',
'HLA-DQA1*04:02-DQB1*06:02', 'HLA-DQA1*04:02-DQB1*06:03', 'HLA-DQA1*04:02-DQB1*06:04', 'HLA-DQA1*04:02-DQB1*06:07', 'HLA-DQA1*04:02-DQB1*06:08',
'HLA-DQA1*04:02-DQB1*06:09', 'HLA-DQA1*04:02-DQB1*06:10', 'HLA-DQA1*04:02-DQB1*06:11', 'HLA-DQA1*04:02-DQB1*06:12', 'HLA-DQA1*04:02-DQB1*06:14',
'HLA-DQA1*04:02-DQB1*06:15', 'HLA-DQA1*04:02-DQB1*06:16', 'HLA-DQA1*04:02-DQB1*06:17', 'HLA-DQA1*04:02-DQB1*06:18', 'HLA-DQA1*04:02-DQB1*06:19',
'HLA-DQA1*04:02-DQB1*06:21', 'HLA-DQA1*04:02-DQB1*06:22', 'HLA-DQA1*04:02-DQB1*06:23', 'HLA-DQA1*04:02-DQB1*06:24', 'HLA-DQA1*04:02-DQB1*06:25',
'HLA-DQA1*04:02-DQB1*06:27', 'HLA-DQA1*04:02-DQB1*06:28', 'HLA-DQA1*04:02-DQB1*06:29', 'HLA-DQA1*04:02-DQB1*06:30', 'HLA-DQA1*04:02-DQB1*06:31',
'HLA-DQA1*04:02-DQB1*06:32', 'HLA-DQA1*04:02-DQB1*06:33', 'HLA-DQA1*04:02-DQB1*06:34', 'HLA-DQA1*04:02-DQB1*06:35', 'HLA-DQA1*04:02-DQB1*06:36',
'HLA-DQA1*04:02-DQB1*06:37', 'HLA-DQA1*04:02-DQB1*06:38', 'HLA-DQA1*04:02-DQB1*06:39', 'HLA-DQA1*04:02-DQB1*06:40', 'HLA-DQA1*04:02-DQB1*06:41',
'HLA-DQA1*04:02-DQB1*06:42', 'HLA-DQA1*04:02-DQB1*06:43', 'HLA-DQA1*04:02-DQB1*06:44', 'HLA-DQA1*04:04-DQB1*02:01', 'HLA-DQA1*04:04-DQB1*02:02',
'HLA-DQA1*04:04-DQB1*02:03', 'HLA-DQA1*04:04-DQB1*02:04', 'HLA-DQA1*04:04-DQB1*02:05', 'HLA-DQA1*04:04-DQB1*02:06', 'HLA-DQA1*04:04-DQB1*03:01',
'HLA-DQA1*04:04-DQB1*03:02', 'HLA-DQA1*04:04-DQB1*03:03', 'HLA-DQA1*04:04-DQB1*03:04', 'HLA-DQA1*04:04-DQB1*03:05', 'HLA-DQA1*04:04-DQB1*03:06',
'HLA-DQA1*04:04-DQB1*03:07', 'HLA-DQA1*04:04-DQB1*03:08', 'HLA-DQA1*04:04-DQB1*03:09', 'HLA-DQA1*04:04-DQB1*03:10', 'HLA-DQA1*04:04-DQB1*03:11',
'HLA-DQA1*04:04-DQB1*03:12', 'HLA-DQA1*04:04-DQB1*03:13', 'HLA-DQA1*04:04-DQB1*03:14', 'HLA-DQA1*04:04-DQB1*03:15', 'HLA-DQA1*04:04-DQB1*03:16',
'HLA-DQA1*04:04-DQB1*03:17', 'HLA-DQA1*04:04-DQB1*03:18', 'HLA-DQA1*04:04-DQB1*03:19', 'HLA-DQA1*04:04-DQB1*03:20', 'HLA-DQA1*04:04-DQB1*03:21',
'HLA-DQA1*04:04-DQB1*03:22', 'HLA-DQA1*04:04-DQB1*03:23', 'HLA-DQA1*04:04-DQB1*03:24', 'HLA-DQA1*04:04-DQB1*03:25', 'HLA-DQA1*04:04-DQB1*03:26',
'HLA-DQA1*04:04-DQB1*03:27', 'HLA-DQA1*04:04-DQB1*03:28', 'HLA-DQA1*04:04-DQB1*03:29', 'HLA-DQA1*04:04-DQB1*03:30', 'HLA-DQA1*04:04-DQB1*03:31',
'HLA-DQA1*04:04-DQB1*03:32', 'HLA-DQA1*04:04-DQB1*03:33', 'HLA-DQA1*04:04-DQB1*03:34', 'HLA-DQA1*04:04-DQB1*03:35', 'HLA-DQA1*04:04-DQB1*03:36',
'HLA-DQA1*04:04-DQB1*03:37', 'HLA-DQA1*04:04-DQB1*03:38', 'HLA-DQA1*04:04-DQB1*04:01', 'HLA-DQA1*04:04-DQB1*04:02', 'HLA-DQA1*04:04-DQB1*04:03',
'HLA-DQA1*04:04-DQB1*04:04', 'HLA-DQA1*04:04-DQB1*04:05', 'HLA-DQA1*04:04-DQB1*04:06', 'HLA-DQA1*04:04-DQB1*04:07', 'HLA-DQA1*04:04-DQB1*04:08',
'HLA-DQA1*04:04-DQB1*05:01', 'HLA-DQA1*04:04-DQB1*05:02', 'HLA-DQA1*04:04-DQB1*05:03', 'HLA-DQA1*04:04-DQB1*05:05', 'HLA-DQA1*04:04-DQB1*05:06',
'HLA-DQA1*04:04-DQB1*05:07', 'HLA-DQA1*04:04-DQB1*05:08', 'HLA-DQA1*04:04-DQB1*05:09', 'HLA-DQA1*04:04-DQB1*05:10', 'HLA-DQA1*04:04-DQB1*05:11',
'HLA-DQA1*04:04-DQB1*05:12', 'HLA-DQA1*04:04-DQB1*05:13', 'HLA-DQA1*04:04-DQB1*05:14', 'HLA-DQA1*04:04-DQB1*06:01', 'HLA-DQA1*04:04-DQB1*06:02',
'HLA-DQA1*04:04-DQB1*06:03', 'HLA-DQA1*04:04-DQB1*06:04', 'HLA-DQA1*04:04-DQB1*06:07', 'HLA-DQA1*04:04-DQB1*06:08', 'HLA-DQA1*04:04-DQB1*06:09',
'HLA-DQA1*04:04-DQB1*06:10', 'HLA-DQA1*04:04-DQB1*06:11', 'HLA-DQA1*04:04-DQB1*06:12', 'HLA-DQA1*04:04-DQB1*06:14', 'HLA-DQA1*04:04-DQB1*06:15',
'HLA-DQA1*04:04-DQB1*06:16', 'HLA-DQA1*04:04-DQB1*06:17', 'HLA-DQA1*04:04-DQB1*06:18', 'HLA-DQA1*04:04-DQB1*06:19', 'HLA-DQA1*04:04-DQB1*06:21',
'HLA-DQA1*04:04-DQB1*06:22', 'HLA-DQA1*04:04-DQB1*06:23', 'HLA-DQA1*04:04-DQB1*06:24', 'HLA-DQA1*04:04-DQB1*06:25', 'HLA-DQA1*04:04-DQB1*06:27',
'HLA-DQA1*04:04-DQB1*06:28', 'HLA-DQA1*04:04-DQB1*06:29', 'HLA-DQA1*04:04-DQB1*06:30', 'HLA-DQA1*04:04-DQB1*06:31', 'HLA-DQA1*04:04-DQB1*06:32',
'HLA-DQA1*04:04-DQB1*06:33', 'HLA-DQA1*04:04-DQB1*06:34', 'HLA-DQA1*04:04-DQB1*06:35', 'HLA-DQA1*04:04-DQB1*06:36', 'HLA-DQA1*04:04-DQB1*06:37',
'HLA-DQA1*04:04-DQB1*06:38', 'HLA-DQA1*04:04-DQB1*06:39', 'HLA-DQA1*04:04-DQB1*06:40', 'HLA-DQA1*04:04-DQB1*06:41', 'HLA-DQA1*04:04-DQB1*06:42',
'HLA-DQA1*04:04-DQB1*06:43', 'HLA-DQA1*04:04-DQB1*06:44', 'HLA-DQA1*05:01-DQB1*02:01', 'HLA-DQA1*05:01-DQB1*02:02', 'HLA-DQA1*05:01-DQB1*02:03',
'HLA-DQA1*05:01-DQB1*02:04', 'HLA-DQA1*05:01-DQB1*02:05', 'HLA-DQA1*05:01-DQB1*02:06', 'HLA-DQA1*05:01-DQB1*03:01', 'HLA-DQA1*05:01-DQB1*03:02',
'HLA-DQA1*05:01-DQB1*03:03', 'HLA-DQA1*05:01-DQB1*03:04', 'HLA-DQA1*05:01-DQB1*03:05', 'HLA-DQA1*05:01-DQB1*03:06', 'HLA-DQA1*05:01-DQB1*03:07',
'HLA-DQA1*05:01-DQB1*03:08', 'HLA-DQA1*05:01-DQB1*03:09', 'HLA-DQA1*05:01-DQB1*03:10', 'HLA-DQA1*05:01-DQB1*03:11', 'HLA-DQA1*05:01-DQB1*03:12',
'HLA-DQA1*05:01-DQB1*03:13', 'HLA-DQA1*05:01-DQB1*03:14', 'HLA-DQA1*05:01-DQB1*03:15', 'HLA-DQA1*05:01-DQB1*03:16', 'HLA-DQA1*05:01-DQB1*03:17',
'HLA-DQA1*05:01-DQB1*03:18', 'HLA-DQA1*05:01-DQB1*03:19', 'HLA-DQA1*05:01-DQB1*03:20', 'HLA-DQA1*05:01-DQB1*03:21', 'HLA-DQA1*05:01-DQB1*03:22',
'HLA-DQA1*05:01-DQB1*03:23', 'HLA-DQA1*05:01-DQB1*03:24', 'HLA-DQA1*05:01-DQB1*03:25', 'HLA-DQA1*05:01-DQB1*03:26', 'HLA-DQA1*05:01-DQB1*03:27',
'HLA-DQA1*05:01-DQB1*03:28', 'HLA-DQA1*05:01-DQB1*03:29', 'HLA-DQA1*05:01-DQB1*03:30', 'HLA-DQA1*05:01-DQB1*03:31', 'HLA-DQA1*05:01-DQB1*03:32',
'HLA-DQA1*05:01-DQB1*03:33', 'HLA-DQA1*05:01-DQB1*03:34', 'HLA-DQA1*05:01-DQB1*03:35', 'HLA-DQA1*05:01-DQB1*03:36', 'HLA-DQA1*05:01-DQB1*03:37',
'HLA-DQA1*05:01-DQB1*03:38', 'HLA-DQA1*05:01-DQB1*04:01', 'HLA-DQA1*05:01-DQB1*04:02', 'HLA-DQA1*05:01-DQB1*04:03', 'HLA-DQA1*05:01-DQB1*04:04',
'HLA-DQA1*05:01-DQB1*04:05', 'HLA-DQA1*05:01-DQB1*04:06', 'HLA-DQA1*05:01-DQB1*04:07', 'HLA-DQA1*05:01-DQB1*04:08', 'HLA-DQA1*05:01-DQB1*05:01',
'HLA-DQA1*05:01-DQB1*05:02', 'HLA-DQA1*05:01-DQB1*05:03', 'HLA-DQA1*05:01-DQB1*05:05', 'HLA-DQA1*05:01-DQB1*05:06', 'HLA-DQA1*05:01-DQB1*05:07',
'HLA-DQA1*05:01-DQB1*05:08', 'HLA-DQA1*05:01-DQB1*05:09', 'HLA-DQA1*05:01-DQB1*05:10', 'HLA-DQA1*05:01-DQB1*05:11', 'HLA-DQA1*05:01-DQB1*05:12',
'HLA-DQA1*05:01-DQB1*05:13', 'HLA-DQA1*05:01-DQB1*05:14', 'HLA-DQA1*05:01-DQB1*06:01', 'HLA-DQA1*05:01-DQB1*06:02', 'HLA-DQA1*05:01-DQB1*06:03',
'HLA-DQA1*05:01-DQB1*06:04', 'HLA-DQA1*05:01-DQB1*06:07', 'HLA-DQA1*05:01-DQB1*06:08', 'HLA-DQA1*05:01-DQB1*06:09', 'HLA-DQA1*05:01-DQB1*06:10',
'HLA-DQA1*05:01-DQB1*06:11', 'HLA-DQA1*05:01-DQB1*06:12', 'HLA-DQA1*05:01-DQB1*06:14', 'HLA-DQA1*05:01-DQB1*06:15', 'HLA-DQA1*05:01-DQB1*06:16',
'HLA-DQA1*05:01-DQB1*06:17', 'HLA-DQA1*05:01-DQB1*06:18', 'HLA-DQA1*05:01-DQB1*06:19', 'HLA-DQA1*05:01-DQB1*06:21', 'HLA-DQA1*05:01-DQB1*06:22',
'HLA-DQA1*05:01-DQB1*06:23', 'HLA-DQA1*05:01-DQB1*06:24', 'HLA-DQA1*05:01-DQB1*06:25', 'HLA-DQA1*05:01-DQB1*06:27', 'HLA-DQA1*05:01-DQB1*06:28',
'HLA-DQA1*05:01-DQB1*06:29', 'HLA-DQA1*05:01-DQB1*06:30', 'HLA-DQA1*05:01-DQB1*06:31', 'HLA-DQA1*05:01-DQB1*06:32', 'HLA-DQA1*05:01-DQB1*06:33',
'HLA-DQA1*05:01-DQB1*06:34', 'HLA-DQA1*05:01-DQB1*06:35', 'HLA-DQA1*05:01-DQB1*06:36', 'HLA-DQA1*05:01-DQB1*06:37', 'HLA-DQA1*05:01-DQB1*06:38',
'HLA-DQA1*05:01-DQB1*06:39', 'HLA-DQA1*05:01-DQB1*06:40', 'HLA-DQA1*05:01-DQB1*06:41', 'HLA-DQA1*05:01-DQB1*06:42', 'HLA-DQA1*05:01-DQB1*06:43',
'HLA-DQA1*05:01-DQB1*06:44', 'HLA-DQA1*05:03-DQB1*02:01', 'HLA-DQA1*05:03-DQB1*02:02', 'HLA-DQA1*05:03-DQB1*02:03', 'HLA-DQA1*05:03-DQB1*02:04',
'HLA-DQA1*05:03-DQB1*02:05', 'HLA-DQA1*05:03-DQB1*02:06', 'HLA-DQA1*05:03-DQB1*03:01', 'HLA-DQA1*05:03-DQB1*03:02', 'HLA-DQA1*05:03-DQB1*03:03',
'HLA-DQA1*05:03-DQB1*03:04', 'HLA-DQA1*05:03-DQB1*03:05', 'HLA-DQA1*05:03-DQB1*03:06', 'HLA-DQA1*05:03-DQB1*03:07', 'HLA-DQA1*05:03-DQB1*03:08',
'HLA-DQA1*05:03-DQB1*03:09', 'HLA-DQA1*05:03-DQB1*03:10', 'HLA-DQA1*05:03-DQB1*03:11', 'HLA-DQA1*05:03-DQB1*03:12', 'HLA-DQA1*05:03-DQB1*03:13',
'HLA-DQA1*05:03-DQB1*03:14', 'HLA-DQA1*05:03-DQB1*03:15', 'HLA-DQA1*05:03-DQB1*03:16', 'HLA-DQA1*05:03-DQB1*03:17', 'HLA-DQA1*05:03-DQB1*03:18',
'HLA-DQA1*05:03-DQB1*03:19', 'HLA-DQA1*05:03-DQB1*03:20', 'HLA-DQA1*05:03-DQB1*03:21', 'HLA-DQA1*05:03-DQB1*03:22', 'HLA-DQA1*05:03-DQB1*03:23',
'HLA-DQA1*05:03-DQB1*03:24', 'HLA-DQA1*05:03-DQB1*03:25', 'HLA-DQA1*05:03-DQB1*03:26', 'HLA-DQA1*05:03-DQB1*03:27', 'HLA-DQA1*05:03-DQB1*03:28',
'HLA-DQA1*05:03-DQB1*03:29', 'HLA-DQA1*05:03-DQB1*03:30', 'HLA-DQA1*05:03-DQB1*03:31', 'HLA-DQA1*05:03-DQB1*03:32', 'HLA-DQA1*05:03-DQB1*03:33',
'HLA-DQA1*05:03-DQB1*03:34', 'HLA-DQA1*05:03-DQB1*03:35', 'HLA-DQA1*05:03-DQB1*03:36', 'HLA-DQA1*05:03-DQB1*03:37', 'HLA-DQA1*05:03-DQB1*03:38',
'HLA-DQA1*05:03-DQB1*04:01', 'HLA-DQA1*05:03-DQB1*04:02', 'HLA-DQA1*05:03-DQB1*04:03', 'HLA-DQA1*05:03-DQB1*04:04', 'HLA-DQA1*05:03-DQB1*04:05',
'HLA-DQA1*05:03-DQB1*04:06', 'HLA-DQA1*05:03-DQB1*04:07', 'HLA-DQA1*05:03-DQB1*04:08', 'HLA-DQA1*05:03-DQB1*05:01', 'HLA-DQA1*05:03-DQB1*05:02',
'HLA-DQA1*05:03-DQB1*05:03', 'HLA-DQA1*05:03-DQB1*05:05', 'HLA-DQA1*05:03-DQB1*05:06', 'HLA-DQA1*05:03-DQB1*05:07', 'HLA-DQA1*05:03-DQB1*05:08',
'HLA-DQA1*05:03-DQB1*05:09', 'HLA-DQA1*05:03-DQB1*05:10', 'HLA-DQA1*05:03-DQB1*05:11', 'HLA-DQA1*05:03-DQB1*05:12', 'HLA-DQA1*05:03-DQB1*05:13',
'HLA-DQA1*05:03-DQB1*05:14', 'HLA-DQA1*05:03-DQB1*06:01', 'HLA-DQA1*05:03-DQB1*06:02', 'HLA-DQA1*05:03-DQB1*06:03', 'HLA-DQA1*05:03-DQB1*06:04',
'HLA-DQA1*05:03-DQB1*06:07', 'HLA-DQA1*05:03-DQB1*06:08', 'HLA-DQA1*05:03-DQB1*06:09', 'HLA-DQA1*05:03-DQB1*06:10', 'HLA-DQA1*05:03-DQB1*06:11',
'HLA-DQA1*05:03-DQB1*06:12', 'HLA-DQA1*05:03-DQB1*06:14', 'HLA-DQA1*05:03-DQB1*06:15', 'HLA-DQA1*05:03-DQB1*06:16', 'HLA-DQA1*05:03-DQB1*06:17',
'HLA-DQA1*05:03-DQB1*06:18', 'HLA-DQA1*05:03-DQB1*06:19', 'HLA-DQA1*05:03-DQB1*06:21', 'HLA-DQA1*05:03-DQB1*06:22', 'HLA-DQA1*05:03-DQB1*06:23',
'HLA-DQA1*05:03-DQB1*06:24', 'HLA-DQA1*05:03-DQB1*06:25', 'HLA-DQA1*05:03-DQB1*06:27', 'HLA-DQA1*05:03-DQB1*06:28', 'HLA-DQA1*05:03-DQB1*06:29',
'HLA-DQA1*05:03-DQB1*06:30', 'HLA-DQA1*05:03-DQB1*06:31', 'HLA-DQA1*05:03-DQB1*06:32', 'HLA-DQA1*05:03-DQB1*06:33', 'HLA-DQA1*05:03-DQB1*06:34',
'HLA-DQA1*05:03-DQB1*06:35', 'HLA-DQA1*05:03-DQB1*06:36', 'HLA-DQA1*05:03-DQB1*06:37', 'HLA-DQA1*05:03-DQB1*06:38', 'HLA-DQA1*05:03-DQB1*06:39',
'HLA-DQA1*05:03-DQB1*06:40', 'HLA-DQA1*05:03-DQB1*06:41', 'HLA-DQA1*05:03-DQB1*06:42', 'HLA-DQA1*05:03-DQB1*06:43', 'HLA-DQA1*05:03-DQB1*06:44',
'HLA-DQA1*05:04-DQB1*02:01', 'HLA-DQA1*05:04-DQB1*02:02', 'HLA-DQA1*05:04-DQB1*02:03', 'HLA-DQA1*05:04-DQB1*02:04', 'HLA-DQA1*05:04-DQB1*02:05',
'HLA-DQA1*05:04-DQB1*02:06', 'HLA-DQA1*05:04-DQB1*03:01', 'HLA-DQA1*05:04-DQB1*03:02', 'HLA-DQA1*05:04-DQB1*03:03', 'HLA-DQA1*05:04-DQB1*03:04',
'HLA-DQA1*05:04-DQB1*03:05', 'HLA-DQA1*05:04-DQB1*03:06', 'HLA-DQA1*05:04-DQB1*03:07', 'HLA-DQA1*05:04-DQB1*03:08', 'HLA-DQA1*05:04-DQB1*03:09',
'HLA-DQA1*05:04-DQB1*03:10', 'HLA-DQA1*05:04-DQB1*03:11', 'HLA-DQA1*05:04-DQB1*03:12', 'HLA-DQA1*05:04-DQB1*03:13', 'HLA-DQA1*05:04-DQB1*03:14',
'HLA-DQA1*05:04-DQB1*03:15', 'HLA-DQA1*05:04-DQB1*03:16', 'HLA-DQA1*05:04-DQB1*03:17', 'HLA-DQA1*05:04-DQB1*03:18', 'HLA-DQA1*05:04-DQB1*03:19',
'HLA-DQA1*05:04-DQB1*03:20', 'HLA-DQA1*05:04-DQB1*03:21', 'HLA-DQA1*05:04-DQB1*03:22', 'HLA-DQA1*05:04-DQB1*03:23', 'HLA-DQA1*05:04-DQB1*03:24',
'HLA-DQA1*05:04-DQB1*03:25', 'HLA-DQA1*05:04-DQB1*03:26', 'HLA-DQA1*05:04-DQB1*03:27', 'HLA-DQA1*05:04-DQB1*03:28', 'HLA-DQA1*05:04-DQB1*03:29',
'HLA-DQA1*05:04-DQB1*03:30', 'HLA-DQA1*05:04-DQB1*03:31', 'HLA-DQA1*05:04-DQB1*03:32', 'HLA-DQA1*05:04-DQB1*03:33', 'HLA-DQA1*05:04-DQB1*03:34',
'HLA-DQA1*05:04-DQB1*03:35', 'HLA-DQA1*05:04-DQB1*03:36', 'HLA-DQA1*05:04-DQB1*03:37', 'HLA-DQA1*05:04-DQB1*03:38', 'HLA-DQA1*05:04-DQB1*04:01',
'HLA-DQA1*05:04-DQB1*04:02', 'HLA-DQA1*05:04-DQB1*04:03', 'HLA-DQA1*05:04-DQB1*04:04', 'HLA-DQA1*05:04-DQB1*04:05', 'HLA-DQA1*05:04-DQB1*04:06',
'HLA-DQA1*05:04-DQB1*04:07', 'HLA-DQA1*05:04-DQB1*04:08', 'HLA-DQA1*05:04-DQB1*05:01', 'HLA-DQA1*05:04-DQB1*05:02', 'HLA-DQA1*05:04-DQB1*05:03',
'HLA-DQA1*05:04-DQB1*05:05', 'HLA-DQA1*05:04-DQB1*05:06', 'HLA-DQA1*05:04-DQB1*05:07', 'HLA-DQA1*05:04-DQB1*05:08', 'HLA-DQA1*05:04-DQB1*05:09',
'HLA-DQA1*05:04-DQB1*05:10', 'HLA-DQA1*05:04-DQB1*05:11', 'HLA-DQA1*05:04-DQB1*05:12', 'HLA-DQA1*05:04-DQB1*05:13', 'HLA-DQA1*05:04-DQB1*05:14',
'HLA-DQA1*05:04-DQB1*06:01', 'HLA-DQA1*05:04-DQB1*06:02', 'HLA-DQA1*05:04-DQB1*06:03', 'HLA-DQA1*05:04-DQB1*06:04', 'HLA-DQA1*05:04-DQB1*06:07',
'HLA-DQA1*05:04-DQB1*06:08', 'HLA-DQA1*05:04-DQB1*06:09', 'HLA-DQA1*05:04-DQB1*06:10', 'HLA-DQA1*05:04-DQB1*06:11', 'HLA-DQA1*05:04-DQB1*06:12',
'HLA-DQA1*05:04-DQB1*06:14', 'HLA-DQA1*05:04-DQB1*06:15', 'HLA-DQA1*05:04-DQB1*06:16', 'HLA-DQA1*05:04-DQB1*06:17', 'HLA-DQA1*05:04-DQB1*06:18',
'HLA-DQA1*05:04-DQB1*06:19', 'HLA-DQA1*05:04-DQB1*06:21', 'HLA-DQA1*05:04-DQB1*06:22', 'HLA-DQA1*05:04-DQB1*06:23', 'HLA-DQA1*05:04-DQB1*06:24',
'HLA-DQA1*05:04-DQB1*06:25', 'HLA-DQA1*05:04-DQB1*06:27', 'HLA-DQA1*05:04-DQB1*06:28', 'HLA-DQA1*05:04-DQB1*06:29', 'HLA-DQA1*05:04-DQB1*06:30',
'HLA-DQA1*05:04-DQB1*06:31', 'HLA-DQA1*05:04-DQB1*06:32', 'HLA-DQA1*05:04-DQB1*06:33', 'HLA-DQA1*05:04-DQB1*06:34', 'HLA-DQA1*05:04-DQB1*06:35',
'HLA-DQA1*05:04-DQB1*06:36', 'HLA-DQA1*05:04-DQB1*06:37', 'HLA-DQA1*05:04-DQB1*06:38', 'HLA-DQA1*05:04-DQB1*06:39', 'HLA-DQA1*05:04-DQB1*06:40',
'HLA-DQA1*05:04-DQB1*06:41', 'HLA-DQA1*05:04-DQB1*06:42', 'HLA-DQA1*05:04-DQB1*06:43', 'HLA-DQA1*05:04-DQB1*06:44', 'HLA-DQA1*05:05-DQB1*02:01',
'HLA-DQA1*05:05-DQB1*02:02', 'HLA-DQA1*05:05-DQB1*02:03', 'HLA-DQA1*05:05-DQB1*02:04', 'HLA-DQA1*05:05-DQB1*02:05', 'HLA-DQA1*05:05-DQB1*02:06',
'HLA-DQA1*05:05-DQB1*03:01', 'HLA-DQA1*05:05-DQB1*03:02', 'HLA-DQA1*05:05-DQB1*03:03', 'HLA-DQA1*05:05-DQB1*03:04', 'HLA-DQA1*05:05-DQB1*03:05',
'HLA-DQA1*05:05-DQB1*03:06', 'HLA-DQA1*05:05-DQB1*03:07', 'HLA-DQA1*05:05-DQB1*03:08', 'HLA-DQA1*05:05-DQB1*03:09', 'HLA-DQA1*05:05-DQB1*03:10',
'HLA-DQA1*05:05-DQB1*03:11', 'HLA-DQA1*05:05-DQB1*03:12', 'HLA-DQA1*05:05-DQB1*03:13', 'HLA-DQA1*05:05-DQB1*03:14', 'HLA-DQA1*05:05-DQB1*03:15',
'HLA-DQA1*05:05-DQB1*03:16', 'HLA-DQA1*05:05-DQB1*03:17', 'HLA-DQA1*05:05-DQB1*03:18', 'HLA-DQA1*05:05-DQB1*03:19', 'HLA-DQA1*05:05-DQB1*03:20',
'HLA-DQA1*05:05-DQB1*03:21', 'HLA-DQA1*05:05-DQB1*03:22', 'HLA-DQA1*05:05-DQB1*03:23', 'HLA-DQA1*05:05-DQB1*03:24', 'HLA-DQA1*05:05-DQB1*03:25',
'HLA-DQA1*05:05-DQB1*03:26', 'HLA-DQA1*05:05-DQB1*03:27', 'HLA-DQA1*05:05-DQB1*03:28', 'HLA-DQA1*05:05-DQB1*03:29', 'HLA-DQA1*05:05-DQB1*03:30',
'HLA-DQA1*05:05-DQB1*03:31', 'HLA-DQA1*05:05-DQB1*03:32', 'HLA-DQA1*05:05-DQB1*03:33', 'HLA-DQA1*05:05-DQB1*03:34', 'HLA-DQA1*05:05-DQB1*03:35',
'HLA-DQA1*05:05-DQB1*03:36', 'HLA-DQA1*05:05-DQB1*03:37', 'HLA-DQA1*05:05-DQB1*03:38', 'HLA-DQA1*05:05-DQB1*04:01', 'HLA-DQA1*05:05-DQB1*04:02',
'HLA-DQA1*05:05-DQB1*04:03', 'HLA-DQA1*05:05-DQB1*04:04', 'HLA-DQA1*05:05-DQB1*04:05', 'HLA-DQA1*05:05-DQB1*04:06', 'HLA-DQA1*05:05-DQB1*04:07',
'HLA-DQA1*05:05-DQB1*04:08', 'HLA-DQA1*05:05-DQB1*05:01', 'HLA-DQA1*05:05-DQB1*05:02', 'HLA-DQA1*05:05-DQB1*05:03', 'HLA-DQA1*05:05-DQB1*05:05',
'HLA-DQA1*05:05-DQB1*05:06', 'HLA-DQA1*05:05-DQB1*05:07', 'HLA-DQA1*05:05-DQB1*05:08', 'HLA-DQA1*05:05-DQB1*05:09', 'HLA-DQA1*05:05-DQB1*05:10',
'HLA-DQA1*05:05-DQB1*05:11', 'HLA-DQA1*05:05-DQB1*05:12', 'HLA-DQA1*05:05-DQB1*05:13', 'HLA-DQA1*05:05-DQB1*05:14', 'HLA-DQA1*05:05-DQB1*06:01',
'HLA-DQA1*05:05-DQB1*06:02', 'HLA-DQA1*05:05-DQB1*06:03', 'HLA-DQA1*05:05-DQB1*06:04', 'HLA-DQA1*05:05-DQB1*06:07', 'HLA-DQA1*05:05-DQB1*06:08',
'HLA-DQA1*05:05-DQB1*06:09', 'HLA-DQA1*05:05-DQB1*06:10', 'HLA-DQA1*05:05-DQB1*06:11', 'HLA-DQA1*05:05-DQB1*06:12', 'HLA-DQA1*05:05-DQB1*06:14',
'HLA-DQA1*05:05-DQB1*06:15', 'HLA-DQA1*05:05-DQB1*06:16', 'HLA-DQA1*05:05-DQB1*06:17', 'HLA-DQA1*05:05-DQB1*06:18', 'HLA-DQA1*05:05-DQB1*06:19',
'HLA-DQA1*05:05-DQB1*06:21', 'HLA-DQA1*05:05-DQB1*06:22', 'HLA-DQA1*05:05-DQB1*06:23', 'HLA-DQA1*05:05-DQB1*06:24', 'HLA-DQA1*05:05-DQB1*06:25',
'HLA-DQA1*05:05-DQB1*06:27', 'HLA-DQA1*05:05-DQB1*06:28', 'HLA-DQA1*05:05-DQB1*06:29', 'HLA-DQA1*05:05-DQB1*06:30', 'HLA-DQA1*05:05-DQB1*06:31',
'HLA-DQA1*05:05-DQB1*06:32', 'HLA-DQA1*05:05-DQB1*06:33', 'HLA-DQA1*05:05-DQB1*06:34', 'HLA-DQA1*05:05-DQB1*06:35', 'HLA-DQA1*05:05-DQB1*06:36',
'HLA-DQA1*05:05-DQB1*06:37', 'HLA-DQA1*05:05-DQB1*06:38', 'HLA-DQA1*05:05-DQB1*06:39', 'HLA-DQA1*05:05-DQB1*06:40', 'HLA-DQA1*05:05-DQB1*06:41',
'HLA-DQA1*05:05-DQB1*06:42', 'HLA-DQA1*05:05-DQB1*06:43', 'HLA-DQA1*05:05-DQB1*06:44', 'HLA-DQA1*05:06-DQB1*02:01', 'HLA-DQA1*05:06-DQB1*02:02',
'HLA-DQA1*05:06-DQB1*02:03', 'HLA-DQA1*05:06-DQB1*02:04', 'HLA-DQA1*05:06-DQB1*02:05', 'HLA-DQA1*05:06-DQB1*02:06', 'HLA-DQA1*05:06-DQB1*03:01',
'HLA-DQA1*05:06-DQB1*03:02', 'HLA-DQA1*05:06-DQB1*03:03', 'HLA-DQA1*05:06-DQB1*03:04', 'HLA-DQA1*05:06-DQB1*03:05', 'HLA-DQA1*05:06-DQB1*03:06',
'HLA-DQA1*05:06-DQB1*03:07', 'HLA-DQA1*05:06-DQB1*03:08', 'HLA-DQA1*05:06-DQB1*03:09', 'HLA-DQA1*05:06-DQB1*03:10', 'HLA-DQA1*05:06-DQB1*03:11',
'HLA-DQA1*05:06-DQB1*03:12', 'HLA-DQA1*05:06-DQB1*03:13', 'HLA-DQA1*05:06-DQB1*03:14', 'HLA-DQA1*05:06-DQB1*03:15', 'HLA-DQA1*05:06-DQB1*03:16',
'HLA-DQA1*05:06-DQB1*03:17', 'HLA-DQA1*05:06-DQB1*03:18', 'HLA-DQA1*05:06-DQB1*03:19', 'HLA-DQA1*05:06-DQB1*03:20', 'HLA-DQA1*05:06-DQB1*03:21',
'HLA-DQA1*05:06-DQB1*03:22', 'HLA-DQA1*05:06-DQB1*03:23', 'HLA-DQA1*05:06-DQB1*03:24', 'HLA-DQA1*05:06-DQB1*03:25', 'HLA-DQA1*05:06-DQB1*03:26',
'HLA-DQA1*05:06-DQB1*03:27', 'HLA-DQA1*05:06-DQB1*03:28', 'HLA-DQA1*05:06-DQB1*03:29', 'HLA-DQA1*05:06-DQB1*03:30', 'HLA-DQA1*05:06-DQB1*03:31',
'HLA-DQA1*05:06-DQB1*03:32', 'HLA-DQA1*05:06-DQB1*03:33', 'HLA-DQA1*05:06-DQB1*03:34', 'HLA-DQA1*05:06-DQB1*03:35', 'HLA-DQA1*05:06-DQB1*03:36',
'HLA-DQA1*05:06-DQB1*03:37', 'HLA-DQA1*05:06-DQB1*03:38', 'HLA-DQA1*05:06-DQB1*04:01', 'HLA-DQA1*05:06-DQB1*04:02', 'HLA-DQA1*05:06-DQB1*04:03',
'HLA-DQA1*05:06-DQB1*04:04', 'HLA-DQA1*05:06-DQB1*04:05', 'HLA-DQA1*05:06-DQB1*04:06', 'HLA-DQA1*05:06-DQB1*04:07', 'HLA-DQA1*05:06-DQB1*04:08',
'HLA-DQA1*05:06-DQB1*05:01', 'HLA-DQA1*05:06-DQB1*05:02', 'HLA-DQA1*05:06-DQB1*05:03', 'HLA-DQA1*05:06-DQB1*05:05', 'HLA-DQA1*05:06-DQB1*05:06',
'HLA-DQA1*05:06-DQB1*05:07', 'HLA-DQA1*05:06-DQB1*05:08', 'HLA-DQA1*05:06-DQB1*05:09', 'HLA-DQA1*05:06-DQB1*05:10', 'HLA-DQA1*05:06-DQB1*05:11',
'HLA-DQA1*05:06-DQB1*05:12', 'HLA-DQA1*05:06-DQB1*05:13', 'HLA-DQA1*05:06-DQB1*05:14', 'HLA-DQA1*05:06-DQB1*06:01', 'HLA-DQA1*05:06-DQB1*06:02',
'HLA-DQA1*05:06-DQB1*06:03', 'HLA-DQA1*05:06-DQB1*06:04', 'HLA-DQA1*05:06-DQB1*06:07', 'HLA-DQA1*05:06-DQB1*06:08', 'HLA-DQA1*05:06-DQB1*06:09',
'HLA-DQA1*05:06-DQB1*06:10', 'HLA-DQA1*05:06-DQB1*06:11', 'HLA-DQA1*05:06-DQB1*06:12', 'HLA-DQA1*05:06-DQB1*06:14', 'HLA-DQA1*05:06-DQB1*06:15',
'HLA-DQA1*05:06-DQB1*06:16', 'HLA-DQA1*05:06-DQB1*06:17', 'HLA-DQA1*05:06-DQB1*06:18', 'HLA-DQA1*05:06-DQB1*06:19', 'HLA-DQA1*05:06-DQB1*06:21',
'HLA-DQA1*05:06-DQB1*06:22', 'HLA-DQA1*05:06-DQB1*06:23', 'HLA-DQA1*05:06-DQB1*06:24', 'HLA-DQA1*05:06-DQB1*06:25', 'HLA-DQA1*05:06-DQB1*06:27',
'HLA-DQA1*05:06-DQB1*06:28', 'HLA-DQA1*05:06-DQB1*06:29', 'HLA-DQA1*05:06-DQB1*06:30', 'HLA-DQA1*05:06-DQB1*06:31', 'HLA-DQA1*05:06-DQB1*06:32',
'HLA-DQA1*05:06-DQB1*06:33', 'HLA-DQA1*05:06-DQB1*06:34', 'HLA-DQA1*05:06-DQB1*06:35', 'HLA-DQA1*05:06-DQB1*06:36', 'HLA-DQA1*05:06-DQB1*06:37',
'HLA-DQA1*05:06-DQB1*06:38', 'HLA-DQA1*05:06-DQB1*06:39', 'HLA-DQA1*05:06-DQB1*06:40', 'HLA-DQA1*05:06-DQB1*06:41', 'HLA-DQA1*05:06-DQB1*06:42',
'HLA-DQA1*05:06-DQB1*06:43', 'HLA-DQA1*05:06-DQB1*06:44', 'HLA-DQA1*05:07-DQB1*02:01', 'HLA-DQA1*05:07-DQB1*02:02', 'HLA-DQA1*05:07-DQB1*02:03',
'HLA-DQA1*05:07-DQB1*02:04', 'HLA-DQA1*05:07-DQB1*02:05', 'HLA-DQA1*05:07-DQB1*02:06', 'HLA-DQA1*05:07-DQB1*03:01', 'HLA-DQA1*05:07-DQB1*03:02',
'HLA-DQA1*05:07-DQB1*03:03', 'HLA-DQA1*05:07-DQB1*03:04', 'HLA-DQA1*05:07-DQB1*03:05', 'HLA-DQA1*05:07-DQB1*03:06', 'HLA-DQA1*05:07-DQB1*03:07',
'HLA-DQA1*05:07-DQB1*03:08', 'HLA-DQA1*05:07-DQB1*03:09', 'HLA-DQA1*05:07-DQB1*03:10', 'HLA-DQA1*05:07-DQB1*03:11', 'HLA-DQA1*05:07-DQB1*03:12',
'HLA-DQA1*05:07-DQB1*03:13', 'HLA-DQA1*05:07-DQB1*03:14', 'HLA-DQA1*05:07-DQB1*03:15', 'HLA-DQA1*05:07-DQB1*03:16', 'HLA-DQA1*05:07-DQB1*03:17',
'HLA-DQA1*05:07-DQB1*03:18', 'HLA-DQA1*05:07-DQB1*03:19', 'HLA-DQA1*05:07-DQB1*03:20', 'HLA-DQA1*05:07-DQB1*03:21', 'HLA-DQA1*05:07-DQB1*03:22',
'HLA-DQA1*05:07-DQB1*03:23', 'HLA-DQA1*05:07-DQB1*03:24', 'HLA-DQA1*05:07-DQB1*03:25', 'HLA-DQA1*05:07-DQB1*03:26', 'HLA-DQA1*05:07-DQB1*03:27',
'HLA-DQA1*05:07-DQB1*03:28', 'HLA-DQA1*05:07-DQB1*03:29', 'HLA-DQA1*05:07-DQB1*03:30', 'HLA-DQA1*05:07-DQB1*03:31', 'HLA-DQA1*05:07-DQB1*03:32',
'HLA-DQA1*05:07-DQB1*03:33', 'HLA-DQA1*05:07-DQB1*03:34', 'HLA-DQA1*05:07-DQB1*03:35', 'HLA-DQA1*05:07-DQB1*03:36', 'HLA-DQA1*05:07-DQB1*03:37',
'HLA-DQA1*05:07-DQB1*03:38', 'HLA-DQA1*05:07-DQB1*04:01', 'HLA-DQA1*05:07-DQB1*04:02', 'HLA-DQA1*05:07-DQB1*04:03', 'HLA-DQA1*05:07-DQB1*04:04',
'HLA-DQA1*05:07-DQB1*04:05', 'HLA-DQA1*05:07-DQB1*04:06', 'HLA-DQA1*05:07-DQB1*04:07', 'HLA-DQA1*05:07-DQB1*04:08', 'HLA-DQA1*05:07-DQB1*05:01',
'HLA-DQA1*05:07-DQB1*05:02', 'HLA-DQA1*05:07-DQB1*05:03', 'HLA-DQA1*05:07-DQB1*05:05', 'HLA-DQA1*05:07-DQB1*05:06', 'HLA-DQA1*05:07-DQB1*05:07',
'HLA-DQA1*05:07-DQB1*05:08', 'HLA-DQA1*05:07-DQB1*05:09', 'HLA-DQA1*05:07-DQB1*05:10', 'HLA-DQA1*05:07-DQB1*05:11', 'HLA-DQA1*05:07-DQB1*05:12',
'HLA-DQA1*05:07-DQB1*05:13', 'HLA-DQA1*05:07-DQB1*05:14', 'HLA-DQA1*05:07-DQB1*06:01', 'HLA-DQA1*05:07-DQB1*06:02', 'HLA-DQA1*05:07-DQB1*06:03',
'HLA-DQA1*05:07-DQB1*06:04', 'HLA-DQA1*05:07-DQB1*06:07', 'HLA-DQA1*05:07-DQB1*06:08', 'HLA-DQA1*05:07-DQB1*06:09', 'HLA-DQA1*05:07-DQB1*06:10',
'HLA-DQA1*05:07-DQB1*06:11', 'HLA-DQA1*05:07-DQB1*06:12', 'HLA-DQA1*05:07-DQB1*06:14', 'HLA-DQA1*05:07-DQB1*06:15', 'HLA-DQA1*05:07-DQB1*06:16',
'HLA-DQA1*05:07-DQB1*06:17', 'HLA-DQA1*05:07-DQB1*06:18', 'HLA-DQA1*05:07-DQB1*06:19', 'HLA-DQA1*05:07-DQB1*06:21', 'HLA-DQA1*05:07-DQB1*06:22',
'HLA-DQA1*05:07-DQB1*06:23', 'HLA-DQA1*05:07-DQB1*06:24', 'HLA-DQA1*05:07-DQB1*06:25', 'HLA-DQA1*05:07-DQB1*06:27', 'HLA-DQA1*05:07-DQB1*06:28',
'HLA-DQA1*05:07-DQB1*06:29', 'HLA-DQA1*05:07-DQB1*06:30', 'HLA-DQA1*05:07-DQB1*06:31', 'HLA-DQA1*05:07-DQB1*06:32', 'HLA-DQA1*05:07-DQB1*06:33',
'HLA-DQA1*05:07-DQB1*06:34', 'HLA-DQA1*05:07-DQB1*06:35', 'HLA-DQA1*05:07-DQB1*06:36', 'HLA-DQA1*05:07-DQB1*06:37', 'HLA-DQA1*05:07-DQB1*06:38',
'HLA-DQA1*05:07-DQB1*06:39', 'HLA-DQA1*05:07-DQB1*06:40', 'HLA-DQA1*05:07-DQB1*06:41', 'HLA-DQA1*05:07-DQB1*06:42', 'HLA-DQA1*05:07-DQB1*06:43',
'HLA-DQA1*05:07-DQB1*06:44', 'HLA-DQA1*05:08-DQB1*02:01', 'HLA-DQA1*05:08-DQB1*02:02', 'HLA-DQA1*05:08-DQB1*02:03', 'HLA-DQA1*05:08-DQB1*02:04',
'HLA-DQA1*05:08-DQB1*02:05', 'HLA-DQA1*05:08-DQB1*02:06', 'HLA-DQA1*05:08-DQB1*03:01', 'HLA-DQA1*05:08-DQB1*03:02', 'HLA-DQA1*05:08-DQB1*03:03',
'HLA-DQA1*05:08-DQB1*03:04', 'HLA-DQA1*05:08-DQB1*03:05', 'HLA-DQA1*05:08-DQB1*03:06', 'HLA-DQA1*05:08-DQB1*03:07', 'HLA-DQA1*05:08-DQB1*03:08',
'HLA-DQA1*05:08-DQB1*03:09', 'HLA-DQA1*05:08-DQB1*03:10', 'HLA-DQA1*05:08-DQB1*03:11', 'HLA-DQA1*05:08-DQB1*03:12', 'HLA-DQA1*05:08-DQB1*03:13',
'HLA-DQA1*05:08-DQB1*03:14', 'HLA-DQA1*05:08-DQB1*03:15', 'HLA-DQA1*05:08-DQB1*03:16', 'HLA-DQA1*05:08-DQB1*03:17', 'HLA-DQA1*05:08-DQB1*03:18',
'HLA-DQA1*05:08-DQB1*03:19', 'HLA-DQA1*05:08-DQB1*03:20', 'HLA-DQA1*05:08-DQB1*03:21', 'HLA-DQA1*05:08-DQB1*03:22', 'HLA-DQA1*05:08-DQB1*03:23',
'HLA-DQA1*05:08-DQB1*03:24', 'HLA-DQA1*05:08-DQB1*03:25', 'HLA-DQA1*05:08-DQB1*03:26', 'HLA-DQA1*05:08-DQB1*03:27', 'HLA-DQA1*05:08-DQB1*03:28',
'HLA-DQA1*05:08-DQB1*03:29', 'HLA-DQA1*05:08-DQB1*03:30', 'HLA-DQA1*05:08-DQB1*03:31', 'HLA-DQA1*05:08-DQB1*03:32', 'HLA-DQA1*05:08-DQB1*03:33',
'HLA-DQA1*05:08-DQB1*03:34', 'HLA-DQA1*05:08-DQB1*03:35', 'HLA-DQA1*05:08-DQB1*03:36', 'HLA-DQA1*05:08-DQB1*03:37', 'HLA-DQA1*05:08-DQB1*03:38',
'HLA-DQA1*05:08-DQB1*04:01', 'HLA-DQA1*05:08-DQB1*04:02', 'HLA-DQA1*05:08-DQB1*04:03', 'HLA-DQA1*05:08-DQB1*04:04', 'HLA-DQA1*05:08-DQB1*04:05',
'HLA-DQA1*05:08-DQB1*04:06', 'HLA-DQA1*05:08-DQB1*04:07', 'HLA-DQA1*05:08-DQB1*04:08', 'HLA-DQA1*05:08-DQB1*05:01', 'HLA-DQA1*05:08-DQB1*05:02',
'HLA-DQA1*05:08-DQB1*05:03', 'HLA-DQA1*05:08-DQB1*05:05', 'HLA-DQA1*05:08-DQB1*05:06', 'HLA-DQA1*05:08-DQB1*05:07', 'HLA-DQA1*05:08-DQB1*05:08',
'HLA-DQA1*05:08-DQB1*05:09', 'HLA-DQA1*05:08-DQB1*05:10', 'HLA-DQA1*05:08-DQB1*05:11', 'HLA-DQA1*05:08-DQB1*05:12', 'HLA-DQA1*05:08-DQB1*05:13',
'HLA-DQA1*05:08-DQB1*05:14', 'HLA-DQA1*05:08-DQB1*06:01', 'HLA-DQA1*05:08-DQB1*06:02', 'HLA-DQA1*05:08-DQB1*06:03', 'HLA-DQA1*05:08-DQB1*06:04',
'HLA-DQA1*05:08-DQB1*06:07', 'HLA-DQA1*05:08-DQB1*06:08', 'HLA-DQA1*05:08-DQB1*06:09', 'HLA-DQA1*05:08-DQB1*06:10', 'HLA-DQA1*05:08-DQB1*06:11',
'HLA-DQA1*05:08-DQB1*06:12', 'HLA-DQA1*05:08-DQB1*06:14', 'HLA-DQA1*05:08-DQB1*06:15', 'HLA-DQA1*05:08-DQB1*06:16', 'HLA-DQA1*05:08-DQB1*06:17',
'HLA-DQA1*05:08-DQB1*06:18', 'HLA-DQA1*05:08-DQB1*06:19', 'HLA-DQA1*05:08-DQB1*06:21', 'HLA-DQA1*05:08-DQB1*06:22', 'HLA-DQA1*05:08-DQB1*06:23',
'HLA-DQA1*05:08-DQB1*06:24', 'HLA-DQA1*05:08-DQB1*06:25', 'HLA-DQA1*05:08-DQB1*06:27', 'HLA-DQA1*05:08-DQB1*06:28', 'HLA-DQA1*05:08-DQB1*06:29',
'HLA-DQA1*05:08-DQB1*06:30', 'HLA-DQA1*05:08-DQB1*06:31', 'HLA-DQA1*05:08-DQB1*06:32', 'HLA-DQA1*05:08-DQB1*06:33', 'HLA-DQA1*05:08-DQB1*06:34',
'HLA-DQA1*05:08-DQB1*06:35', 'HLA-DQA1*05:08-DQB1*06:36', 'HLA-DQA1*05:08-DQB1*06:37', 'HLA-DQA1*05:08-DQB1*06:38', 'HLA-DQA1*05:08-DQB1*06:39',
'HLA-DQA1*05:08-DQB1*06:40', 'HLA-DQA1*05:08-DQB1*06:41', 'HLA-DQA1*05:08-DQB1*06:42', 'HLA-DQA1*05:08-DQB1*06:43', 'HLA-DQA1*05:08-DQB1*06:44',
'HLA-DQA1*05:09-DQB1*02:01', 'HLA-DQA1*05:09-DQB1*02:02', 'HLA-DQA1*05:09-DQB1*02:03', 'HLA-DQA1*05:09-DQB1*02:04', 'HLA-DQA1*05:09-DQB1*02:05',
'HLA-DQA1*05:09-DQB1*02:06', 'HLA-DQA1*05:09-DQB1*03:01', 'HLA-DQA1*05:09-DQB1*03:02', 'HLA-DQA1*05:09-DQB1*03:03', 'HLA-DQA1*05:09-DQB1*03:04',
'HLA-DQA1*05:09-DQB1*03:05', 'HLA-DQA1*05:09-DQB1*03:06', 'HLA-DQA1*05:09-DQB1*03:07', 'HLA-DQA1*05:09-DQB1*03:08', 'HLA-DQA1*05:09-DQB1*03:09',
'HLA-DQA1*05:09-DQB1*03:10', 'HLA-DQA1*05:09-DQB1*03:11', 'HLA-DQA1*05:09-DQB1*03:12', 'HLA-DQA1*05:09-DQB1*03:13', 'HLA-DQA1*05:09-DQB1*03:14',
'HLA-DQA1*05:09-DQB1*03:15', 'HLA-DQA1*05:09-DQB1*03:16', 'HLA-DQA1*05:09-DQB1*03:17', 'HLA-DQA1*05:09-DQB1*03:18', 'HLA-DQA1*05:09-DQB1*03:19',
'HLA-DQA1*05:09-DQB1*03:20', 'HLA-DQA1*05:09-DQB1*03:21', 'HLA-DQA1*05:09-DQB1*03:22', 'HLA-DQA1*05:09-DQB1*03:23', 'HLA-DQA1*05:09-DQB1*03:24',
'HLA-DQA1*05:09-DQB1*03:25', 'HLA-DQA1*05:09-DQB1*03:26', 'HLA-DQA1*05:09-DQB1*03:27', 'HLA-DQA1*05:09-DQB1*03:28', 'HLA-DQA1*05:09-DQB1*03:29',
'HLA-DQA1*05:09-DQB1*03:30', 'HLA-DQA1*05:09-DQB1*03:31', 'HLA-DQA1*05:09-DQB1*03:32', 'HLA-DQA1*05:09-DQB1*03:33', 'HLA-DQA1*05:09-DQB1*03:34',
'HLA-DQA1*05:09-DQB1*03:35', 'HLA-DQA1*05:09-DQB1*03:36', 'HLA-DQA1*05:09-DQB1*03:37', 'HLA-DQA1*05:09-DQB1*03:38', 'HLA-DQA1*05:09-DQB1*04:01',
'HLA-DQA1*05:09-DQB1*04:02', 'HLA-DQA1*05:09-DQB1*04:03', 'HLA-DQA1*05:09-DQB1*04:04', 'HLA-DQA1*05:09-DQB1*04:05', 'HLA-DQA1*05:09-DQB1*04:06',
'HLA-DQA1*05:09-DQB1*04:07', 'HLA-DQA1*05:09-DQB1*04:08', 'HLA-DQA1*05:09-DQB1*05:01', 'HLA-DQA1*05:09-DQB1*05:02', 'HLA-DQA1*05:09-DQB1*05:03',
'HLA-DQA1*05:09-DQB1*05:05', 'HLA-DQA1*05:09-DQB1*05:06', 'HLA-DQA1*05:09-DQB1*05:07', 'HLA-DQA1*05:09-DQB1*05:08', 'HLA-DQA1*05:09-DQB1*05:09',
'HLA-DQA1*05:09-DQB1*05:10', 'HLA-DQA1*05:09-DQB1*05:11', 'HLA-DQA1*05:09-DQB1*05:12', 'HLA-DQA1*05:09-DQB1*05:13', 'HLA-DQA1*05:09-DQB1*05:14',
'HLA-DQA1*05:09-DQB1*06:01', 'HLA-DQA1*05:09-DQB1*06:02', 'HLA-DQA1*05:09-DQB1*06:03', 'HLA-DQA1*05:09-DQB1*06:04', 'HLA-DQA1*05:09-DQB1*06:07',
'HLA-DQA1*05:09-DQB1*06:08', 'HLA-DQA1*05:09-DQB1*06:09', 'HLA-DQA1*05:09-DQB1*06:10', 'HLA-DQA1*05:09-DQB1*06:11', 'HLA-DQA1*05:09-DQB1*06:12',
'HLA-DQA1*05:09-DQB1*06:14', 'HLA-DQA1*05:09-DQB1*06:15', 'HLA-DQA1*05:09-DQB1*06:16', 'HLA-DQA1*05:09-DQB1*06:17', 'HLA-DQA1*05:09-DQB1*06:18',
'HLA-DQA1*05:09-DQB1*06:19', 'HLA-DQA1*05:09-DQB1*06:21', 'HLA-DQA1*05:09-DQB1*06:22', 'HLA-DQA1*05:09-DQB1*06:23', 'HLA-DQA1*05:09-DQB1*06:24',
'HLA-DQA1*05:09-DQB1*06:25', 'HLA-DQA1*05:09-DQB1*06:27', 'HLA-DQA1*05:09-DQB1*06:28', 'HLA-DQA1*05:09-DQB1*06:29', 'HLA-DQA1*05:09-DQB1*06:30',
'HLA-DQA1*05:09-DQB1*06:31', 'HLA-DQA1*05:09-DQB1*06:32', 'HLA-DQA1*05:09-DQB1*06:33', 'HLA-DQA1*05:09-DQB1*06:34', 'HLA-DQA1*05:09-DQB1*06:35',
'HLA-DQA1*05:09-DQB1*06:36', 'HLA-DQA1*05:09-DQB1*06:37', 'HLA-DQA1*05:09-DQB1*06:38', 'HLA-DQA1*05:09-DQB1*06:39', 'HLA-DQA1*05:09-DQB1*06:40',
'HLA-DQA1*05:09-DQB1*06:41', 'HLA-DQA1*05:09-DQB1*06:42', 'HLA-DQA1*05:09-DQB1*06:43', 'HLA-DQA1*05:09-DQB1*06:44', 'HLA-DQA1*05:10-DQB1*02:01',
'HLA-DQA1*05:10-DQB1*02:02', 'HLA-DQA1*05:10-DQB1*02:03', 'HLA-DQA1*05:10-DQB1*02:04', 'HLA-DQA1*05:10-DQB1*02:05', 'HLA-DQA1*05:10-DQB1*02:06',
'HLA-DQA1*05:10-DQB1*03:01', 'HLA-DQA1*05:10-DQB1*03:02', 'HLA-DQA1*05:10-DQB1*03:03', 'HLA-DQA1*05:10-DQB1*03:04', 'HLA-DQA1*05:10-DQB1*03:05',
'HLA-DQA1*05:10-DQB1*03:06', 'HLA-DQA1*05:10-DQB1*03:07', 'HLA-DQA1*05:10-DQB1*03:08', 'HLA-DQA1*05:10-DQB1*03:09', 'HLA-DQA1*05:10-DQB1*03:10',
'HLA-DQA1*05:10-DQB1*03:11', 'HLA-DQA1*05:10-DQB1*03:12', 'HLA-DQA1*05:10-DQB1*03:13', 'HLA-DQA1*05:10-DQB1*03:14', 'HLA-DQA1*05:10-DQB1*03:15',
'HLA-DQA1*05:10-DQB1*03:16', 'HLA-DQA1*05:10-DQB1*03:17', 'HLA-DQA1*05:10-DQB1*03:18', 'HLA-DQA1*05:10-DQB1*03:19', 'HLA-DQA1*05:10-DQB1*03:20',
'HLA-DQA1*05:10-DQB1*03:21', 'HLA-DQA1*05:10-DQB1*03:22', 'HLA-DQA1*05:10-DQB1*03:23', 'HLA-DQA1*05:10-DQB1*03:24', 'HLA-DQA1*05:10-DQB1*03:25',
'HLA-DQA1*05:10-DQB1*03:26', 'HLA-DQA1*05:10-DQB1*03:27', 'HLA-DQA1*05:10-DQB1*03:28', 'HLA-DQA1*05:10-DQB1*03:29', 'HLA-DQA1*05:10-DQB1*03:30',
'HLA-DQA1*05:10-DQB1*03:31', 'HLA-DQA1*05:10-DQB1*03:32', 'HLA-DQA1*05:10-DQB1*03:33', 'HLA-DQA1*05:10-DQB1*03:34', 'HLA-DQA1*05:10-DQB1*03:35',
'HLA-DQA1*05:10-DQB1*03:36', 'HLA-DQA1*05:10-DQB1*03:37', 'HLA-DQA1*05:10-DQB1*03:38', 'HLA-DQA1*05:10-DQB1*04:01', 'HLA-DQA1*05:10-DQB1*04:02',
'HLA-DQA1*05:10-DQB1*04:03', 'HLA-DQA1*05:10-DQB1*04:04', 'HLA-DQA1*05:10-DQB1*04:05', 'HLA-DQA1*05:10-DQB1*04:06', 'HLA-DQA1*05:10-DQB1*04:07',
'HLA-DQA1*05:10-DQB1*04:08', 'HLA-DQA1*05:10-DQB1*05:01', 'HLA-DQA1*05:10-DQB1*05:02', 'HLA-DQA1*05:10-DQB1*05:03', 'HLA-DQA1*05:10-DQB1*05:05',
'HLA-DQA1*05:10-DQB1*05:06', 'HLA-DQA1*05:10-DQB1*05:07', 'HLA-DQA1*05:10-DQB1*05:08', 'HLA-DQA1*05:10-DQB1*05:09', 'HLA-DQA1*05:10-DQB1*05:10',
'HLA-DQA1*05:10-DQB1*05:11', 'HLA-DQA1*05:10-DQB1*05:12', 'HLA-DQA1*05:10-DQB1*05:13', 'HLA-DQA1*05:10-DQB1*05:14', 'HLA-DQA1*05:10-DQB1*06:01',
'HLA-DQA1*05:10-DQB1*06:02', 'HLA-DQA1*05:10-DQB1*06:03', 'HLA-DQA1*05:10-DQB1*06:04', 'HLA-DQA1*05:10-DQB1*06:07', 'HLA-DQA1*05:10-DQB1*06:08',
'HLA-DQA1*05:10-DQB1*06:09', 'HLA-DQA1*05:10-DQB1*06:10', 'HLA-DQA1*05:10-DQB1*06:11', 'HLA-DQA1*05:10-DQB1*06:12', 'HLA-DQA1*05:10-DQB1*06:14',
'HLA-DQA1*05:10-DQB1*06:15', 'HLA-DQA1*05:10-DQB1*06:16', 'HLA-DQA1*05:10-DQB1*06:17', 'HLA-DQA1*05:10-DQB1*06:18', 'HLA-DQA1*05:10-DQB1*06:19',
'HLA-DQA1*05:10-DQB1*06:21', 'HLA-DQA1*05:10-DQB1*06:22', 'HLA-DQA1*05:10-DQB1*06:23', 'HLA-DQA1*05:10-DQB1*06:24', 'HLA-DQA1*05:10-DQB1*06:25',
'HLA-DQA1*05:10-DQB1*06:27', 'HLA-DQA1*05:10-DQB1*06:28', 'HLA-DQA1*05:10-DQB1*06:29', 'HLA-DQA1*05:10-DQB1*06:30', 'HLA-DQA1*05:10-DQB1*06:31',
'HLA-DQA1*05:10-DQB1*06:32', 'HLA-DQA1*05:10-DQB1*06:33', 'HLA-DQA1*05:10-DQB1*06:34', 'HLA-DQA1*05:10-DQB1*06:35', 'HLA-DQA1*05:10-DQB1*06:36',
'HLA-DQA1*05:10-DQB1*06:37', 'HLA-DQA1*05:10-DQB1*06:38', 'HLA-DQA1*05:10-DQB1*06:39', 'HLA-DQA1*05:10-DQB1*06:40', 'HLA-DQA1*05:10-DQB1*06:41',
'HLA-DQA1*05:10-DQB1*06:42', 'HLA-DQA1*05:10-DQB1*06:43', 'HLA-DQA1*05:10-DQB1*06:44', 'HLA-DQA1*05:11-DQB1*02:01', 'HLA-DQA1*05:11-DQB1*02:02',
'HLA-DQA1*05:11-DQB1*02:03', 'HLA-DQA1*05:11-DQB1*02:04', 'HLA-DQA1*05:11-DQB1*02:05', 'HLA-DQA1*05:11-DQB1*02:06', 'HLA-DQA1*05:11-DQB1*03:01',
'HLA-DQA1*05:11-DQB1*03:02', 'HLA-DQA1*05:11-DQB1*03:03', 'HLA-DQA1*05:11-DQB1*03:04', 'HLA-DQA1*05:11-DQB1*03:05', 'HLA-DQA1*05:11-DQB1*03:06',
'HLA-DQA1*05:11-DQB1*03:07', 'HLA-DQA1*05:11-DQB1*03:08', 'HLA-DQA1*05:11-DQB1*03:09', 'HLA-DQA1*05:11-DQB1*03:10', 'HLA-DQA1*05:11-DQB1*03:11',
'HLA-DQA1*05:11-DQB1*03:12', 'HLA-DQA1*05:11-DQB1*03:13', 'HLA-DQA1*05:11-DQB1*03:14', 'HLA-DQA1*05:11-DQB1*03:15', 'HLA-DQA1*05:11-DQB1*03:16',
'HLA-DQA1*05:11-DQB1*03:17', 'HLA-DQA1*05:11-DQB1*03:18', 'HLA-DQA1*05:11-DQB1*03:19', 'HLA-DQA1*05:11-DQB1*03:20', 'HLA-DQA1*05:11-DQB1*03:21',
'HLA-DQA1*05:11-DQB1*03:22', 'HLA-DQA1*05:11-DQB1*03:23', 'HLA-DQA1*05:11-DQB1*03:24', 'HLA-DQA1*05:11-DQB1*03:25', 'HLA-DQA1*05:11-DQB1*03:26',
'HLA-DQA1*05:11-DQB1*03:27', 'HLA-DQA1*05:11-DQB1*03:28', 'HLA-DQA1*05:11-DQB1*03:29', 'HLA-DQA1*05:11-DQB1*03:30', 'HLA-DQA1*05:11-DQB1*03:31',
'HLA-DQA1*05:11-DQB1*03:32', 'HLA-DQA1*05:11-DQB1*03:33', 'HLA-DQA1*05:11-DQB1*03:34', 'HLA-DQA1*05:11-DQB1*03:35', 'HLA-DQA1*05:11-DQB1*03:36',
'HLA-DQA1*05:11-DQB1*03:37', 'HLA-DQA1*05:11-DQB1*03:38', 'HLA-DQA1*05:11-DQB1*04:01', 'HLA-DQA1*05:11-DQB1*04:02', 'HLA-DQA1*05:11-DQB1*04:03',
'HLA-DQA1*05:11-DQB1*04:04', 'HLA-DQA1*05:11-DQB1*04:05', 'HLA-DQA1*05:11-DQB1*04:06', 'HLA-DQA1*05:11-DQB1*04:07', 'HLA-DQA1*05:11-DQB1*04:08',
'HLA-DQA1*05:11-DQB1*05:01', 'HLA-DQA1*05:11-DQB1*05:02', 'HLA-DQA1*05:11-DQB1*05:03', 'HLA-DQA1*05:11-DQB1*05:05', 'HLA-DQA1*05:11-DQB1*05:06',
'HLA-DQA1*05:11-DQB1*05:07', 'HLA-DQA1*05:11-DQB1*05:08', 'HLA-DQA1*05:11-DQB1*05:09', 'HLA-DQA1*05:11-DQB1*05:10', 'HLA-DQA1*05:11-DQB1*05:11',
'HLA-DQA1*05:11-DQB1*05:12', 'HLA-DQA1*05:11-DQB1*05:13', 'HLA-DQA1*05:11-DQB1*05:14', 'HLA-DQA1*05:11-DQB1*06:01', 'HLA-DQA1*05:11-DQB1*06:02',
'HLA-DQA1*05:11-DQB1*06:03', 'HLA-DQA1*05:11-DQB1*06:04', 'HLA-DQA1*05:11-DQB1*06:07', 'HLA-DQA1*05:11-DQB1*06:08', 'HLA-DQA1*05:11-DQB1*06:09',
'HLA-DQA1*05:11-DQB1*06:10', 'HLA-DQA1*05:11-DQB1*06:11', 'HLA-DQA1*05:11-DQB1*06:12', 'HLA-DQA1*05:11-DQB1*06:14', 'HLA-DQA1*05:11-DQB1*06:15',
'HLA-DQA1*05:11-DQB1*06:16', 'HLA-DQA1*05:11-DQB1*06:17', 'HLA-DQA1*05:11-DQB1*06:18', 'HLA-DQA1*05:11-DQB1*06:19', 'HLA-DQA1*05:11-DQB1*06:21',
'HLA-DQA1*05:11-DQB1*06:22', 'HLA-DQA1*05:11-DQB1*06:23', 'HLA-DQA1*05:11-DQB1*06:24', 'HLA-DQA1*05:11-DQB1*06:25', 'HLA-DQA1*05:11-DQB1*06:27',
'HLA-DQA1*05:11-DQB1*06:28', 'HLA-DQA1*05:11-DQB1*06:29', 'HLA-DQA1*05:11-DQB1*06:30', 'HLA-DQA1*05:11-DQB1*06:31', 'HLA-DQA1*05:11-DQB1*06:32',
'HLA-DQA1*05:11-DQB1*06:33', 'HLA-DQA1*05:11-DQB1*06:34', 'HLA-DQA1*05:11-DQB1*06:35', 'HLA-DQA1*05:11-DQB1*06:36', 'HLA-DQA1*05:11-DQB1*06:37',
'HLA-DQA1*05:11-DQB1*06:38', 'HLA-DQA1*05:11-DQB1*06:39', 'HLA-DQA1*05:11-DQB1*06:40', 'HLA-DQA1*05:11-DQB1*06:41', 'HLA-DQA1*05:11-DQB1*06:42',
'HLA-DQA1*05:11-DQB1*06:43', 'HLA-DQA1*05:11-DQB1*06:44', 'HLA-DQA1*06:01-DQB1*02:01', 'HLA-DQA1*06:01-DQB1*02:02', 'HLA-DQA1*06:01-DQB1*02:03',
'HLA-DQA1*06:01-DQB1*02:04', 'HLA-DQA1*06:01-DQB1*02:05', 'HLA-DQA1*06:01-DQB1*02:06', 'HLA-DQA1*06:01-DQB1*03:01', 'HLA-DQA1*06:01-DQB1*03:02',
'HLA-DQA1*06:01-DQB1*03:03', 'HLA-DQA1*06:01-DQB1*03:04', 'HLA-DQA1*06:01-DQB1*03:05', 'HLA-DQA1*06:01-DQB1*03:06', 'HLA-DQA1*06:01-DQB1*03:07',
'HLA-DQA1*06:01-DQB1*03:08', 'HLA-DQA1*06:01-DQB1*03:09', 'HLA-DQA1*06:01-DQB1*03:10', 'HLA-DQA1*06:01-DQB1*03:11', 'HLA-DQA1*06:01-DQB1*03:12',
'HLA-DQA1*06:01-DQB1*03:13', 'HLA-DQA1*06:01-DQB1*03:14', 'HLA-DQA1*06:01-DQB1*03:15', 'HLA-DQA1*06:01-DQB1*03:16', 'HLA-DQA1*06:01-DQB1*03:17',
'HLA-DQA1*06:01-DQB1*03:18', 'HLA-DQA1*06:01-DQB1*03:19', 'HLA-DQA1*06:01-DQB1*03:20', 'HLA-DQA1*06:01-DQB1*03:21', 'HLA-DQA1*06:01-DQB1*03:22',
'HLA-DQA1*06:01-DQB1*03:23', 'HLA-DQA1*06:01-DQB1*03:24', 'HLA-DQA1*06:01-DQB1*03:25', 'HLA-DQA1*06:01-DQB1*03:26', 'HLA-DQA1*06:01-DQB1*03:27',
'HLA-DQA1*06:01-DQB1*03:28', 'HLA-DQA1*06:01-DQB1*03:29', 'HLA-DQA1*06:01-DQB1*03:30', 'HLA-DQA1*06:01-DQB1*03:31', 'HLA-DQA1*06:01-DQB1*03:32',
'HLA-DQA1*06:01-DQB1*03:33', 'HLA-DQA1*06:01-DQB1*03:34', 'HLA-DQA1*06:01-DQB1*03:35', 'HLA-DQA1*06:01-DQB1*03:36', 'HLA-DQA1*06:01-DQB1*03:37',
'HLA-DQA1*06:01-DQB1*03:38', 'HLA-DQA1*06:01-DQB1*04:01', 'HLA-DQA1*06:01-DQB1*04:02', 'HLA-DQA1*06:01-DQB1*04:03', 'HLA-DQA1*06:01-DQB1*04:04',
'HLA-DQA1*06:01-DQB1*04:05', 'HLA-DQA1*06:01-DQB1*04:06', 'HLA-DQA1*06:01-DQB1*04:07', 'HLA-DQA1*06:01-DQB1*04:08', 'HLA-DQA1*06:01-DQB1*05:01',
'HLA-DQA1*06:01-DQB1*05:02', 'HLA-DQA1*06:01-DQB1*05:03', 'HLA-DQA1*06:01-DQB1*05:05', 'HLA-DQA1*06:01-DQB1*05:06', 'HLA-DQA1*06:01-DQB1*05:07',
'HLA-DQA1*06:01-DQB1*05:08', 'HLA-DQA1*06:01-DQB1*05:09', 'HLA-DQA1*06:01-DQB1*05:10', 'HLA-DQA1*06:01-DQB1*05:11', 'HLA-DQA1*06:01-DQB1*05:12',
'HLA-DQA1*06:01-DQB1*05:13', 'HLA-DQA1*06:01-DQB1*05:14', 'HLA-DQA1*06:01-DQB1*06:01', 'HLA-DQA1*06:01-DQB1*06:02', 'HLA-DQA1*06:01-DQB1*06:03',
'HLA-DQA1*06:01-DQB1*06:04', 'HLA-DQA1*06:01-DQB1*06:07', 'HLA-DQA1*06:01-DQB1*06:08', 'HLA-DQA1*06:01-DQB1*06:09', 'HLA-DQA1*06:01-DQB1*06:10',
'HLA-DQA1*06:01-DQB1*06:11', 'HLA-DQA1*06:01-DQB1*06:12', 'HLA-DQA1*06:01-DQB1*06:14', 'HLA-DQA1*06:01-DQB1*06:15', 'HLA-DQA1*06:01-DQB1*06:16',
'HLA-DQA1*06:01-DQB1*06:17', 'HLA-DQA1*06:01-DQB1*06:18', 'HLA-DQA1*06:01-DQB1*06:19', 'HLA-DQA1*06:01-DQB1*06:21', 'HLA-DQA1*06:01-DQB1*06:22',
'HLA-DQA1*06:01-DQB1*06:23', 'HLA-DQA1*06:01-DQB1*06:24', 'HLA-DQA1*06:01-DQB1*06:25', 'HLA-DQA1*06:01-DQB1*06:27', 'HLA-DQA1*06:01-DQB1*06:28',
'HLA-DQA1*06:01-DQB1*06:29', 'HLA-DQA1*06:01-DQB1*06:30', 'HLA-DQA1*06:01-DQB1*06:31', 'HLA-DQA1*06:01-DQB1*06:32', 'HLA-DQA1*06:01-DQB1*06:33',
'HLA-DQA1*06:01-DQB1*06:34', 'HLA-DQA1*06:01-DQB1*06:35', 'HLA-DQA1*06:01-DQB1*06:36', 'HLA-DQA1*06:01-DQB1*06:37', 'HLA-DQA1*06:01-DQB1*06:38',
'HLA-DQA1*06:01-DQB1*06:39', 'HLA-DQA1*06:01-DQB1*06:40', 'HLA-DQA1*06:01-DQB1*06:41', 'HLA-DQA1*06:01-DQB1*06:42', 'HLA-DQA1*06:01-DQB1*06:43',
'HLA-DQA1*06:01-DQB1*06:44', 'HLA-DQA1*06:02-DQB1*02:01', 'HLA-DQA1*06:02-DQB1*02:02', 'HLA-DQA1*06:02-DQB1*02:03', 'HLA-DQA1*06:02-DQB1*02:04',
'HLA-DQA1*06:02-DQB1*02:05', 'HLA-DQA1*06:02-DQB1*02:06', 'HLA-DQA1*06:02-DQB1*03:01', 'HLA-DQA1*06:02-DQB1*03:02', 'HLA-DQA1*06:02-DQB1*03:03',
'HLA-DQA1*06:02-DQB1*03:04', 'HLA-DQA1*06:02-DQB1*03:05', 'HLA-DQA1*06:02-DQB1*03:06', 'HLA-DQA1*06:02-DQB1*03:07', 'HLA-DQA1*06:02-DQB1*03:08',
'HLA-DQA1*06:02-DQB1*03:09', 'HLA-DQA1*06:02-DQB1*03:10', 'HLA-DQA1*06:02-DQB1*03:11', 'HLA-DQA1*06:02-DQB1*03:12', 'HLA-DQA1*06:02-DQB1*03:13',
'HLA-DQA1*06:02-DQB1*03:14', 'HLA-DQA1*06:02-DQB1*03:15', 'HLA-DQA1*06:02-DQB1*03:16', 'HLA-DQA1*06:02-DQB1*03:17', 'HLA-DQA1*06:02-DQB1*03:18',
'HLA-DQA1*06:02-DQB1*03:19', 'HLA-DQA1*06:02-DQB1*03:20', 'HLA-DQA1*06:02-DQB1*03:21', 'HLA-DQA1*06:02-DQB1*03:22', 'HLA-DQA1*06:02-DQB1*03:23',
'HLA-DQA1*06:02-DQB1*03:24', 'HLA-DQA1*06:02-DQB1*03:25', 'HLA-DQA1*06:02-DQB1*03:26', 'HLA-DQA1*06:02-DQB1*03:27', 'HLA-DQA1*06:02-DQB1*03:28',
'HLA-DQA1*06:02-DQB1*03:29', 'HLA-DQA1*06:02-DQB1*03:30', 'HLA-DQA1*06:02-DQB1*03:31', 'HLA-DQA1*06:02-DQB1*03:32', 'HLA-DQA1*06:02-DQB1*03:33',
'HLA-DQA1*06:02-DQB1*03:34', 'HLA-DQA1*06:02-DQB1*03:35', 'HLA-DQA1*06:02-DQB1*03:36', 'HLA-DQA1*06:02-DQB1*03:37', 'HLA-DQA1*06:02-DQB1*03:38',
'HLA-DQA1*06:02-DQB1*04:01', 'HLA-DQA1*06:02-DQB1*04:02', 'HLA-DQA1*06:02-DQB1*04:03', 'HLA-DQA1*06:02-DQB1*04:04', 'HLA-DQA1*06:02-DQB1*04:05',
'HLA-DQA1*06:02-DQB1*04:06', 'HLA-DQA1*06:02-DQB1*04:07', 'HLA-DQA1*06:02-DQB1*04:08', 'HLA-DQA1*06:02-DQB1*05:01', 'HLA-DQA1*06:02-DQB1*05:02',
'HLA-DQA1*06:02-DQB1*05:03', 'HLA-DQA1*06:02-DQB1*05:05', 'HLA-DQA1*06:02-DQB1*05:06', 'HLA-DQA1*06:02-DQB1*05:07', 'HLA-DQA1*06:02-DQB1*05:08',
'HLA-DQA1*06:02-DQB1*05:09', 'HLA-DQA1*06:02-DQB1*05:10', 'HLA-DQA1*06:02-DQB1*05:11', 'HLA-DQA1*06:02-DQB1*05:12', 'HLA-DQA1*06:02-DQB1*05:13',
'HLA-DQA1*06:02-DQB1*05:14', 'HLA-DQA1*06:02-DQB1*06:01', 'HLA-DQA1*06:02-DQB1*06:02', 'HLA-DQA1*06:02-DQB1*06:03', 'HLA-DQA1*06:02-DQB1*06:04',
'HLA-DQA1*06:02-DQB1*06:07', 'HLA-DQA1*06:02-DQB1*06:08', 'HLA-DQA1*06:02-DQB1*06:09', 'HLA-DQA1*06:02-DQB1*06:10', 'HLA-DQA1*06:02-DQB1*06:11',
'HLA-DQA1*06:02-DQB1*06:12', 'HLA-DQA1*06:02-DQB1*06:14', 'HLA-DQA1*06:02-DQB1*06:15', 'HLA-DQA1*06:02-DQB1*06:16', 'HLA-DQA1*06:02-DQB1*06:17',
'HLA-DQA1*06:02-DQB1*06:18', 'HLA-DQA1*06:02-DQB1*06:19', 'HLA-DQA1*06:02-DQB1*06:21', 'HLA-DQA1*06:02-DQB1*06:22', 'HLA-DQA1*06:02-DQB1*06:23',
'HLA-DQA1*06:02-DQB1*06:24', 'HLA-DQA1*06:02-DQB1*06:25', 'HLA-DQA1*06:02-DQB1*06:27', 'HLA-DQA1*06:02-DQB1*06:28', 'HLA-DQA1*06:02-DQB1*06:29',
'HLA-DQA1*06:02-DQB1*06:30', 'HLA-DQA1*06:02-DQB1*06:31', 'HLA-DQA1*06:02-DQB1*06:32', 'HLA-DQA1*06:02-DQB1*06:33', 'HLA-DQA1*06:02-DQB1*06:34',
'HLA-DQA1*06:02-DQB1*06:35', 'HLA-DQA1*06:02-DQB1*06:36', 'HLA-DQA1*06:02-DQB1*06:37', 'HLA-DQA1*06:02-DQB1*06:38', 'HLA-DQA1*06:02-DQB1*06:39',
'HLA-DQA1*06:02-DQB1*06:40', 'HLA-DQA1*06:02-DQB1*06:41', 'HLA-DQA1*06:02-DQB1*06:42', 'HLA-DQA1*06:02-DQB1*06:43', 'HLA-DQA1*06:02-DQB1*06:44',
'H-2-Iab', 'H-2-Iad', 'H-2-Iak', 'H-2-Iaq', 'H-2-Ias',
'H-2-Iau', 'H-2-Iad', 'H-2-Iak'])

    __version = "4.0"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """A list of valid :class:`~epytope.Core.Allele.Allele` models"""
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in set([x for x in next(f) if x != ""])]
        next(f)
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCIIPAN_4_0]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCIIPAN_4_0 + i * Offset.NETMHCIIPAN_4_0])
                ranks[a][pep_seq] = float(row[RankIndex.NETMHCIIPAN_4_0 + i * Offset.NETMHCIIPAN_4_0])
                # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}

        return result

class NetMHCIIpan_4_1(NetMHCIIpan_4_0):
    """
    Implementation of NetMHCIIpan 4.1 adapter.
    """

    __command = "netMHCIIpan -f {peptides} -inptype 1 -a {alleles} {options} -xls -xlsfile {out}"
    __version = "4.1"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command 

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        f = csv.reader(open(file, "r"), delimiter='\t')
        scores = defaultdict(defaultdict)
        ranks = defaultdict(defaultdict)
        alleles = [x for x in set([x for x in next(f) if x != ""])]
        next(f)
        for row in f:
            pep_seq = row[PeptideIndex.NETMHCIIPAN_4_1]
            for i, a in enumerate(alleles):
                scores[a][pep_seq] = float(row[ScoreIndex.NETMHCIIPAN_4_1 + i * Offset.NETMHCIIPAN_4_1])
                ranks[a][pep_seq] = float(row[RankIndex.NETMHCIIPAN_4_1 + i * Offset.NETMHCIIPAN_4_1])
                # Create dictionary with hierarchy: {'Allele1': {'Score': {'Pep1': Score1, 'Pep2': Score2,..}, 'Rank': {'Pep1': RankScore1, 'Pep2': RankScore2,..}}, 'Allele2':...}
        result = {allele: {metric:(list(scores.values())[j] if metric == "Score" else list(ranks.values())[j]) for metric in ["Score", "Rank"]} for j, allele in enumerate(alleles)}

        return result

class PickPocket_1_1(AExternalEpitopePrediction):
    """
    Implementation of PickPocket adapter.

    .. note::

        Zhang, H., Lund, O., & Nielsen, M. (2009). The PickPocket method for predicting binding specificities
        for receptors based on receptor pocket similarities: application to MHC-peptide binding.
        Bioinformatics, 25(10), 1293-1299.

    """
    __name = "pickpocket"
    __supported_length = frozenset([8, 9, 10, 11])
    __command = 'PickPocket -p {peptides} -a {alleles} {options} | grep -v "#" > {out}'
    __supported_alleles = frozenset(['HLA-A*01:01', 'HLA-A*01:02', 'HLA-A*01:03', 'HLA-A*01:06', 'HLA-A*01:07', 'HLA-A*01:08', 'HLA-A*01:09',
                                     'HLA-A*01:10', 'HLA-A*01:12', 'HLA-A*01:13', 'HLA-A*01:14', 'HLA-A*01:17', 'HLA-A*01:19', 'HLA-A*01:20',
                                     'HLA-A*01:21', 'HLA-A*01:23', 'HLA-A*01:24',
                                     'HLA-A*01:25', 'HLA-A*01:26', 'HLA-A*01:28', 'HLA-A*01:29', 'HLA-A*01:30', 'HLA-A*01:32', 'HLA-A*01:33',
                                     'HLA-A*01:35', 'HLA-A*01:36', 'HLA-A*01:37',
                                     'HLA-A*01:38', 'HLA-A*01:39', 'HLA-A*01:40', 'HLA-A*01:41', 'HLA-A*01:42', 'HLA-A*01:43', 'HLA-A*01:44',
                                     'HLA-A*01:45', 'HLA-A*01:46', 'HLA-A*01:47',
                                     'HLA-A*01:48', 'HLA-A*01:49', 'HLA-A*01:50', 'HLA-A*01:51', 'HLA-A*01:54', 'HLA-A*01:55', 'HLA-A*01:58',
                                     'HLA-A*01:59', 'HLA-A*01:60', 'HLA-A*01:61',
                                     'HLA-A*01:62', 'HLA-A*01:63', 'HLA-A*01:64', 'HLA-A*01:65', 'HLA-A*01:66', 'HLA-A*02:01', 'HLA-A*02:02',
                                     'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:05',
                                     'HLA-A*02:06', 'HLA-A*02:07', 'HLA-A*02:08', 'HLA-A*02:09', 'HLA-A*02:10', 'HLA-A*02:11', 'HLA-A*02:12',
                                     'HLA-A*02:13', 'HLA-A*02:14', 'HLA-A*02:16',
                                     'HLA-A*02:17', 'HLA-A*02:18', 'HLA-A*02:19', 'HLA-A*02:20', 'HLA-A*02:21', 'HLA-A*02:22', 'HLA-A*02:24',
                                     'HLA-A*02:25', 'HLA-A*02:26', 'HLA-A*02:27',
                                     'HLA-A*02:28', 'HLA-A*02:29', 'HLA-A*02:30', 'HLA-A*02:31', 'HLA-A*02:33', 'HLA-A*02:34', 'HLA-A*02:35',
                                     'HLA-A*02:36', 'HLA-A*02:37', 'HLA-A*02:38',
                                     'HLA-A*02:39', 'HLA-A*02:40', 'HLA-A*02:41', 'HLA-A*02:42', 'HLA-A*02:44', 'HLA-A*02:45', 'HLA-A*02:46',
                                     'HLA-A*02:47', 'HLA-A*02:48', 'HLA-A*02:49',
                                     'HLA-A*02:50', 'HLA-A*02:51', 'HLA-A*02:52', 'HLA-A*02:54', 'HLA-A*02:55', 'HLA-A*02:56', 'HLA-A*02:57',
                                     'HLA-A*02:58', 'HLA-A*02:59', 'HLA-A*02:60',
                                     'HLA-A*02:61', 'HLA-A*02:62', 'HLA-A*02:63', 'HLA-A*02:64', 'HLA-A*02:65', 'HLA-A*02:66', 'HLA-A*02:67',
                                     'HLA-A*02:68', 'HLA-A*02:69', 'HLA-A*02:70',
                                     'HLA-A*02:71', 'HLA-A*02:72', 'HLA-A*02:73', 'HLA-A*02:74', 'HLA-A*02:75', 'HLA-A*02:76', 'HLA-A*02:77',
                                     'HLA-A*02:78', 'HLA-A*02:79', 'HLA-A*02:80',
                                     'HLA-A*02:81', 'HLA-A*02:84', 'HLA-A*02:85', 'HLA-A*02:86', 'HLA-A*02:87', 'HLA-A*02:89', 'HLA-A*02:90',
                                     'HLA-A*02:91', 'HLA-A*02:92', 'HLA-A*02:93',
                                     'HLA-A*02:95', 'HLA-A*02:96', 'HLA-A*02:97', 'HLA-A*02:99', 'HLA-A*02:101', 'HLA-A*02:102', 'HLA-A*02:103',
                                     'HLA-A*02:104', 'HLA-A*02:105',
                                     'HLA-A*02:106', 'HLA-A*02:107', 'HLA-A*02:108', 'HLA-A*02:109', 'HLA-A*02:110', 'HLA-A*02:111', 'HLA-A*02:112',
                                     'HLA-A*02:114', 'HLA-A*02:115',
                                     'HLA-A*02:116', 'HLA-A*02:117', 'HLA-A*02:118', 'HLA-A*02:119', 'HLA-A*02:120', 'HLA-A*02:121', 'HLA-A*02:122',
                                     'HLA-A*02:123', 'HLA-A*02:124',
                                     'HLA-A*02:126', 'HLA-A*02:127', 'HLA-A*02:128', 'HLA-A*02:129', 'HLA-A*02:130', 'HLA-A*02:131', 'HLA-A*02:132',
                                     'HLA-A*02:133', 'HLA-A*02:134',
                                     'HLA-A*02:135', 'HLA-A*02:136', 'HLA-A*02:137', 'HLA-A*02:138', 'HLA-A*02:139', 'HLA-A*02:140', 'HLA-A*02:141',
                                     'HLA-A*02:142', 'HLA-A*02:143',
                                     'HLA-A*02:144', 'HLA-A*02:145', 'HLA-A*02:146', 'HLA-A*02:147', 'HLA-A*02:148', 'HLA-A*02:149', 'HLA-A*02:150',
                                     'HLA-A*02:151', 'HLA-A*02:152',
                                     'HLA-A*02:153', 'HLA-A*02:154', 'HLA-A*02:155', 'HLA-A*02:156', 'HLA-A*02:157', 'HLA-A*02:158', 'HLA-A*02:159',
                                     'HLA-A*02:160', 'HLA-A*02:161',
                                     'HLA-A*02:162', 'HLA-A*02:163', 'HLA-A*02:164', 'HLA-A*02:165', 'HLA-A*02:166', 'HLA-A*02:167', 'HLA-A*02:168',
                                     'HLA-A*02:169', 'HLA-A*02:170',
                                     'HLA-A*02:171', 'HLA-A*02:172', 'HLA-A*02:173', 'HLA-A*02:174', 'HLA-A*02:175', 'HLA-A*02:176', 'HLA-A*02:177',
                                     'HLA-A*02:178', 'HLA-A*02:179',
                                     'HLA-A*02:180', 'HLA-A*02:181', 'HLA-A*02:182', 'HLA-A*02:183', 'HLA-A*02:184', 'HLA-A*02:185', 'HLA-A*02:186',
                                     'HLA-A*02:187', 'HLA-A*02:188',
                                     'HLA-A*02:189', 'HLA-A*02:190', 'HLA-A*02:191', 'HLA-A*02:192', 'HLA-A*02:193', 'HLA-A*02:194', 'HLA-A*02:195',
                                     'HLA-A*02:196', 'HLA-A*02:197',
                                     'HLA-A*02:198', 'HLA-A*02:199', 'HLA-A*02:200', 'HLA-A*02:201', 'HLA-A*02:202', 'HLA-A*02:203', 'HLA-A*02:204',
                                     'HLA-A*02:205', 'HLA-A*02:206',
                                     'HLA-A*02:207', 'HLA-A*02:208', 'HLA-A*02:209', 'HLA-A*02:210', 'HLA-A*02:211', 'HLA-A*02:212', 'HLA-A*02:213',
                                     'HLA-A*02:214', 'HLA-A*02:215',
                                     'HLA-A*02:216', 'HLA-A*02:217', 'HLA-A*02:218', 'HLA-A*02:219', 'HLA-A*02:220', 'HLA-A*02:221', 'HLA-A*02:224',
                                     'HLA-A*02:228', 'HLA-A*02:229',
                                     'HLA-A*02:230', 'HLA-A*02:231', 'HLA-A*02:232', 'HLA-A*02:233', 'HLA-A*02:234', 'HLA-A*02:235', 'HLA-A*02:236',
                                     'HLA-A*02:237', 'HLA-A*02:238',
                                     'HLA-A*02:239', 'HLA-A*02:240', 'HLA-A*02:241', 'HLA-A*02:242', 'HLA-A*02:243', 'HLA-A*02:244', 'HLA-A*02:245',
                                     'HLA-A*02:246', 'HLA-A*02:247',
                                     'HLA-A*02:248', 'HLA-A*02:249', 'HLA-A*02:251', 'HLA-A*02:252', 'HLA-A*02:253', 'HLA-A*02:254', 'HLA-A*02:255',
                                     'HLA-A*02:256', 'HLA-A*02:257',
                                     'HLA-A*02:258', 'HLA-A*02:259', 'HLA-A*02:260', 'HLA-A*02:261', 'HLA-A*02:262', 'HLA-A*02:263', 'HLA-A*02:264',
                                     'HLA-A*02:265', 'HLA-A*02:266',
                                     'HLA-A*03:01', 'HLA-A*03:02', 'HLA-A*03:04', 'HLA-A*03:05', 'HLA-A*03:06', 'HLA-A*03:07', 'HLA-A*03:08',
                                     'HLA-A*03:09', 'HLA-A*03:10', 'HLA-A*03:12',
                                     'HLA-A*03:13', 'HLA-A*03:14', 'HLA-A*03:15', 'HLA-A*03:16', 'HLA-A*03:17', 'HLA-A*03:18', 'HLA-A*03:19',
                                     'HLA-A*03:20', 'HLA-A*03:22', 'HLA-A*03:23',
                                     'HLA-A*03:24', 'HLA-A*03:25', 'HLA-A*03:26', 'HLA-A*03:27', 'HLA-A*03:28', 'HLA-A*03:29', 'HLA-A*03:30',
                                     'HLA-A*03:31', 'HLA-A*03:32', 'HLA-A*03:33',
                                     'HLA-A*03:34', 'HLA-A*03:35', 'HLA-A*03:37', 'HLA-A*03:38', 'HLA-A*03:39', 'HLA-A*03:40', 'HLA-A*03:41',
                                     'HLA-A*03:42', 'HLA-A*03:43', 'HLA-A*03:44',
                                     'HLA-A*03:45', 'HLA-A*03:46', 'HLA-A*03:47', 'HLA-A*03:48', 'HLA-A*03:49', 'HLA-A*03:50', 'HLA-A*03:51',
                                     'HLA-A*03:52', 'HLA-A*03:53', 'HLA-A*03:54',
                                     'HLA-A*03:55', 'HLA-A*03:56', 'HLA-A*03:57', 'HLA-A*03:58', 'HLA-A*03:59', 'HLA-A*03:60', 'HLA-A*03:61',
                                     'HLA-A*03:62', 'HLA-A*03:63', 'HLA-A*03:64',
                                     'HLA-A*03:65', 'HLA-A*03:66', 'HLA-A*03:67', 'HLA-A*03:70', 'HLA-A*03:71', 'HLA-A*03:72', 'HLA-A*03:73',
                                     'HLA-A*03:74', 'HLA-A*03:75', 'HLA-A*03:76',
                                     'HLA-A*03:77', 'HLA-A*03:78', 'HLA-A*03:79', 'HLA-A*03:80', 'HLA-A*03:81', 'HLA-A*03:82', 'HLA-A*11:01',
                                     'HLA-A*11:02', 'HLA-A*11:03', 'HLA-A*11:04',
                                     'HLA-A*11:05', 'HLA-A*11:06', 'HLA-A*11:07', 'HLA-A*11:08', 'HLA-A*11:09', 'HLA-A*11:10', 'HLA-A*11:11',
                                     'HLA-A*11:12', 'HLA-A*11:13', 'HLA-A*11:14',
                                     'HLA-A*11:15', 'HLA-A*11:16', 'HLA-A*11:17', 'HLA-A*11:18', 'HLA-A*11:19', 'HLA-A*11:20', 'HLA-A*11:22',
                                     'HLA-A*11:23', 'HLA-A*11:24', 'HLA-A*11:25',
                                     'HLA-A*11:26', 'HLA-A*11:27', 'HLA-A*11:29', 'HLA-A*11:30', 'HLA-A*11:31', 'HLA-A*11:32', 'HLA-A*11:33',
                                     'HLA-A*11:34', 'HLA-A*11:35', 'HLA-A*11:36',
                                     'HLA-A*11:37', 'HLA-A*11:38', 'HLA-A*11:39', 'HLA-A*11:40', 'HLA-A*11:41', 'HLA-A*11:42', 'HLA-A*11:43',
                                     'HLA-A*11:44', 'HLA-A*11:45', 'HLA-A*11:46',
                                     'HLA-A*11:47', 'HLA-A*11:48', 'HLA-A*11:49', 'HLA-A*11:51', 'HLA-A*11:53', 'HLA-A*11:54', 'HLA-A*11:55',
                                     'HLA-A*11:56', 'HLA-A*11:57', 'HLA-A*11:58',
                                     'HLA-A*11:59', 'HLA-A*11:60', 'HLA-A*11:61', 'HLA-A*11:62', 'HLA-A*11:63', 'HLA-A*11:64', 'HLA-A*23:01',
                                     'HLA-A*23:02', 'HLA-A*23:03', 'HLA-A*23:04',
                                     'HLA-A*23:05', 'HLA-A*23:06', 'HLA-A*23:09', 'HLA-A*23:10', 'HLA-A*23:12', 'HLA-A*23:13', 'HLA-A*23:14',
                                     'HLA-A*23:15', 'HLA-A*23:16', 'HLA-A*23:17',
                                     'HLA-A*23:18', 'HLA-A*23:20', 'HLA-A*23:21', 'HLA-A*23:22', 'HLA-A*23:23', 'HLA-A*23:24', 'HLA-A*23:25',
                                     'HLA-A*23:26', 'HLA-A*24:02', 'HLA-A*24:03',
                                     'HLA-A*24:04', 'HLA-A*24:05', 'HLA-A*24:06', 'HLA-A*24:07', 'HLA-A*24:08', 'HLA-A*24:10', 'HLA-A*24:13',
                                     'HLA-A*24:14', 'HLA-A*24:15', 'HLA-A*24:17',
                                     'HLA-A*24:18', 'HLA-A*24:19', 'HLA-A*24:20', 'HLA-A*24:21', 'HLA-A*24:22', 'HLA-A*24:23', 'HLA-A*24:24',
                                     'HLA-A*24:25', 'HLA-A*24:26', 'HLA-A*24:27',
                                     'HLA-A*24:28', 'HLA-A*24:29', 'HLA-A*24:30', 'HLA-A*24:31', 'HLA-A*24:32', 'HLA-A*24:33', 'HLA-A*24:34',
                                     'HLA-A*24:35', 'HLA-A*24:37', 'HLA-A*24:38',
                                     'HLA-A*24:39', 'HLA-A*24:41', 'HLA-A*24:42', 'HLA-A*24:43', 'HLA-A*24:44', 'HLA-A*24:46', 'HLA-A*24:47',
                                     'HLA-A*24:49', 'HLA-A*24:50', 'HLA-A*24:51',
                                     'HLA-A*24:52', 'HLA-A*24:53', 'HLA-A*24:54', 'HLA-A*24:55', 'HLA-A*24:56', 'HLA-A*24:57', 'HLA-A*24:58',
                                     'HLA-A*24:59', 'HLA-A*24:61', 'HLA-A*24:62',
                                     'HLA-A*24:63', 'HLA-A*24:64', 'HLA-A*24:66', 'HLA-A*24:67', 'HLA-A*24:68', 'HLA-A*24:69', 'HLA-A*24:70',
                                     'HLA-A*24:71', 'HLA-A*24:72', 'HLA-A*24:73',
                                     'HLA-A*24:74', 'HLA-A*24:75', 'HLA-A*24:76', 'HLA-A*24:77', 'HLA-A*24:78', 'HLA-A*24:79', 'HLA-A*24:80',
                                     'HLA-A*24:81', 'HLA-A*24:82', 'HLA-A*24:85',
                                     'HLA-A*24:87', 'HLA-A*24:88', 'HLA-A*24:89', 'HLA-A*24:91', 'HLA-A*24:92', 'HLA-A*24:93', 'HLA-A*24:94',
                                     'HLA-A*24:95', 'HLA-A*24:96', 'HLA-A*24:97',
                                     'HLA-A*24:98', 'HLA-A*24:99', 'HLA-A*24:100', 'HLA-A*24:101', 'HLA-A*24:102', 'HLA-A*24:103', 'HLA-A*24:104',
                                     'HLA-A*24:105', 'HLA-A*24:106',
                                     'HLA-A*24:107', 'HLA-A*24:108', 'HLA-A*24:109', 'HLA-A*24:110', 'HLA-A*24:111', 'HLA-A*24:112', 'HLA-A*24:113',
                                     'HLA-A*24:114', 'HLA-A*24:115',
                                     'HLA-A*24:116', 'HLA-A*24:117', 'HLA-A*24:118', 'HLA-A*24:119', 'HLA-A*24:120', 'HLA-A*24:121', 'HLA-A*24:122',
                                     'HLA-A*24:123', 'HLA-A*24:124',
                                     'HLA-A*24:125', 'HLA-A*24:126', 'HLA-A*24:127', 'HLA-A*24:128', 'HLA-A*24:129', 'HLA-A*24:130', 'HLA-A*24:131',
                                     'HLA-A*24:133', 'HLA-A*24:134',
                                     'HLA-A*24:135', 'HLA-A*24:136', 'HLA-A*24:137', 'HLA-A*24:138', 'HLA-A*24:139', 'HLA-A*24:140', 'HLA-A*24:141',
                                     'HLA-A*24:142', 'HLA-A*24:143',
                                     'HLA-A*24:144', 'HLA-A*25:01', 'HLA-A*25:02', 'HLA-A*25:03', 'HLA-A*25:04', 'HLA-A*25:05', 'HLA-A*25:06',
                                     'HLA-A*25:07', 'HLA-A*25:08', 'HLA-A*25:09',
                                     'HLA-A*25:10', 'HLA-A*25:11', 'HLA-A*25:13', 'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*26:04',
                                     'HLA-A*26:05', 'HLA-A*26:06', 'HLA-A*26:07',
                                     'HLA-A*26:08', 'HLA-A*26:09', 'HLA-A*26:10', 'HLA-A*26:12', 'HLA-A*26:13', 'HLA-A*26:14', 'HLA-A*26:15',
                                     'HLA-A*26:16', 'HLA-A*26:17', 'HLA-A*26:18',
                                     'HLA-A*26:19', 'HLA-A*26:20', 'HLA-A*26:21', 'HLA-A*26:22', 'HLA-A*26:23', 'HLA-A*26:24', 'HLA-A*26:26',
                                     'HLA-A*26:27', 'HLA-A*26:28', 'HLA-A*26:29',
                                     'HLA-A*26:30', 'HLA-A*26:31', 'HLA-A*26:32', 'HLA-A*26:33', 'HLA-A*26:34', 'HLA-A*26:35', 'HLA-A*26:36',
                                     'HLA-A*26:37', 'HLA-A*26:38', 'HLA-A*26:39',
                                     'HLA-A*26:40', 'HLA-A*26:41', 'HLA-A*26:42', 'HLA-A*26:43', 'HLA-A*26:45', 'HLA-A*26:46', 'HLA-A*26:47',
                                     'HLA-A*26:48', 'HLA-A*26:49', 'HLA-A*26:50',
                                     'HLA-A*29:01', 'HLA-A*29:02', 'HLA-A*29:03', 'HLA-A*29:04', 'HLA-A*29:05', 'HLA-A*29:06', 'HLA-A*29:07',
                                     'HLA-A*29:09', 'HLA-A*29:10', 'HLA-A*29:11',
                                     'HLA-A*29:12', 'HLA-A*29:13', 'HLA-A*29:14', 'HLA-A*29:15', 'HLA-A*29:16', 'HLA-A*29:17', 'HLA-A*29:18',
                                     'HLA-A*29:19', 'HLA-A*29:20', 'HLA-A*29:21',
                                     'HLA-A*29:22', 'HLA-A*30:01', 'HLA-A*30:02', 'HLA-A*30:03', 'HLA-A*30:04', 'HLA-A*30:06', 'HLA-A*30:07',
                                     'HLA-A*30:08', 'HLA-A*30:09', 'HLA-A*30:10',
                                     'HLA-A*30:11', 'HLA-A*30:12', 'HLA-A*30:13', 'HLA-A*30:15', 'HLA-A*30:16', 'HLA-A*30:17', 'HLA-A*30:18',
                                     'HLA-A*30:19', 'HLA-A*30:20', 'HLA-A*30:22',
                                     'HLA-A*30:23', 'HLA-A*30:24', 'HLA-A*30:25', 'HLA-A*30:26', 'HLA-A*30:28', 'HLA-A*30:29', 'HLA-A*30:30',
                                     'HLA-A*30:31', 'HLA-A*30:32', 'HLA-A*30:33',
                                     'HLA-A*30:34', 'HLA-A*30:35', 'HLA-A*30:36', 'HLA-A*30:37', 'HLA-A*30:38', 'HLA-A*30:39', 'HLA-A*30:40',
                                     'HLA-A*30:41', 'HLA-A*31:01', 'HLA-A*31:02',
                                     'HLA-A*31:03', 'HLA-A*31:04', 'HLA-A*31:05', 'HLA-A*31:06', 'HLA-A*31:07', 'HLA-A*31:08', 'HLA-A*31:09',
                                     'HLA-A*31:10', 'HLA-A*31:11', 'HLA-A*31:12',
                                     'HLA-A*31:13', 'HLA-A*31:15', 'HLA-A*31:16', 'HLA-A*31:17', 'HLA-A*31:18', 'HLA-A*31:19', 'HLA-A*31:20',
                                     'HLA-A*31:21', 'HLA-A*31:22', 'HLA-A*31:23',
                                     'HLA-A*31:24', 'HLA-A*31:25', 'HLA-A*31:26', 'HLA-A*31:27', 'HLA-A*31:28', 'HLA-A*31:29', 'HLA-A*31:30',
                                     'HLA-A*31:31', 'HLA-A*31:32', 'HLA-A*31:33',
                                     'HLA-A*31:34', 'HLA-A*31:35', 'HLA-A*31:36', 'HLA-A*31:37', 'HLA-A*32:01', 'HLA-A*32:02', 'HLA-A*32:03',
                                     'HLA-A*32:04', 'HLA-A*32:05', 'HLA-A*32:06',
                                     'HLA-A*32:07', 'HLA-A*32:08', 'HLA-A*32:09', 'HLA-A*32:10', 'HLA-A*32:12', 'HLA-A*32:13', 'HLA-A*32:14',
                                     'HLA-A*32:15', 'HLA-A*32:16', 'HLA-A*32:17',
                                     'HLA-A*32:18', 'HLA-A*32:20', 'HLA-A*32:21', 'HLA-A*32:22', 'HLA-A*32:23', 'HLA-A*32:24', 'HLA-A*32:25',
                                     'HLA-A*33:01', 'HLA-A*33:03', 'HLA-A*33:04',
                                     'HLA-A*33:05', 'HLA-A*33:06', 'HLA-A*33:07', 'HLA-A*33:08', 'HLA-A*33:09', 'HLA-A*33:10', 'HLA-A*33:11',
                                     'HLA-A*33:12', 'HLA-A*33:13', 'HLA-A*33:14',
                                     'HLA-A*33:15', 'HLA-A*33:16', 'HLA-A*33:17', 'HLA-A*33:18', 'HLA-A*33:19', 'HLA-A*33:20', 'HLA-A*33:21',
                                     'HLA-A*33:22', 'HLA-A*33:23', 'HLA-A*33:24',
                                     'HLA-A*33:25', 'HLA-A*33:26', 'HLA-A*33:27', 'HLA-A*33:28', 'HLA-A*33:29', 'HLA-A*33:30', 'HLA-A*33:31',
                                     'HLA-A*34:01', 'HLA-A*34:02', 'HLA-A*34:03',
                                     'HLA-A*34:04', 'HLA-A*34:05', 'HLA-A*34:06', 'HLA-A*34:07', 'HLA-A*34:08', 'HLA-A*36:01', 'HLA-A*36:02',
                                     'HLA-A*36:03', 'HLA-A*36:04', 'HLA-A*36:05',
                                     'HLA-A*43:01', 'HLA-A*66:01', 'HLA-A*66:02', 'HLA-A*66:03', 'HLA-A*66:04', 'HLA-A*66:05', 'HLA-A*66:06',
                                     'HLA-A*66:07', 'HLA-A*66:08', 'HLA-A*66:09',
                                     'HLA-A*66:10', 'HLA-A*66:11', 'HLA-A*66:12', 'HLA-A*66:13', 'HLA-A*66:14', 'HLA-A*66:15', 'HLA-A*68:01',
                                     'HLA-A*68:02', 'HLA-A*68:03', 'HLA-A*68:04',
                                     'HLA-A*68:05', 'HLA-A*68:06', 'HLA-A*68:07', 'HLA-A*68:08', 'HLA-A*68:09', 'HLA-A*68:10', 'HLA-A*68:12',
                                     'HLA-A*68:13', 'HLA-A*68:14', 'HLA-A*68:15',
                                     'HLA-A*68:16', 'HLA-A*68:17', 'HLA-A*68:19', 'HLA-A*68:20', 'HLA-A*68:21', 'HLA-A*68:22', 'HLA-A*68:23',
                                     'HLA-A*68:24', 'HLA-A*68:25', 'HLA-A*68:26',
                                     'HLA-A*68:27', 'HLA-A*68:28', 'HLA-A*68:29', 'HLA-A*68:30', 'HLA-A*68:31', 'HLA-A*68:32', 'HLA-A*68:33',
                                     'HLA-A*68:34', 'HLA-A*68:35', 'HLA-A*68:36',
                                     'HLA-A*68:37', 'HLA-A*68:38', 'HLA-A*68:39', 'HLA-A*68:40', 'HLA-A*68:41', 'HLA-A*68:42', 'HLA-A*68:43',
                                     'HLA-A*68:44', 'HLA-A*68:45', 'HLA-A*68:46',
                                     'HLA-A*68:47', 'HLA-A*68:48', 'HLA-A*68:50', 'HLA-A*68:51', 'HLA-A*68:52', 'HLA-A*68:53', 'HLA-A*68:54',
                                     'HLA-A*69:01', 'HLA-A*74:01', 'HLA-A*74:02',
                                     'HLA-A*74:03', 'HLA-A*74:04', 'HLA-A*74:05', 'HLA-A*74:06', 'HLA-A*74:07', 'HLA-A*74:08', 'HLA-A*74:09',
                                     'HLA-A*74:10', 'HLA-A*74:11', 'HLA-A*74:13',
                                     'HLA-A*80:01', 'HLA-A*80:02', 'HLA-B*07:02', 'HLA-B*07:03', 'HLA-B*07:04', 'HLA-B*07:05', 'HLA-B*07:06',
                                     'HLA-B*07:07', 'HLA-B*07:08', 'HLA-B*07:09',
                                     'HLA-B*07:10', 'HLA-B*07:11', 'HLA-B*07:12', 'HLA-B*07:13', 'HLA-B*07:14', 'HLA-B*07:15', 'HLA-B*07:16',
                                     'HLA-B*07:17', 'HLA-B*07:18', 'HLA-B*07:19',
                                     'HLA-B*07:20', 'HLA-B*07:21', 'HLA-B*07:22', 'HLA-B*07:23', 'HLA-B*07:24', 'HLA-B*07:25', 'HLA-B*07:26',
                                     'HLA-B*07:27', 'HLA-B*07:28', 'HLA-B*07:29',
                                     'HLA-B*07:30', 'HLA-B*07:31', 'HLA-B*07:32', 'HLA-B*07:33', 'HLA-B*07:34', 'HLA-B*07:35', 'HLA-B*07:36',
                                     'HLA-B*07:37', 'HLA-B*07:38', 'HLA-B*07:39',
                                     'HLA-B*07:40', 'HLA-B*07:41', 'HLA-B*07:42', 'HLA-B*07:43', 'HLA-B*07:44', 'HLA-B*07:45', 'HLA-B*07:46',
                                     'HLA-B*07:47', 'HLA-B*07:48', 'HLA-B*07:50',
                                     'HLA-B*07:51', 'HLA-B*07:52', 'HLA-B*07:53', 'HLA-B*07:54', 'HLA-B*07:55', 'HLA-B*07:56', 'HLA-B*07:57',
                                     'HLA-B*07:58', 'HLA-B*07:59', 'HLA-B*07:60',
                                     'HLA-B*07:61', 'HLA-B*07:62', 'HLA-B*07:63', 'HLA-B*07:64', 'HLA-B*07:65', 'HLA-B*07:66', 'HLA-B*07:68',
                                     'HLA-B*07:69', 'HLA-B*07:70', 'HLA-B*07:71',
                                     'HLA-B*07:72', 'HLA-B*07:73', 'HLA-B*07:74', 'HLA-B*07:75', 'HLA-B*07:76', 'HLA-B*07:77', 'HLA-B*07:78',
                                     'HLA-B*07:79', 'HLA-B*07:80', 'HLA-B*07:81',
                                     'HLA-B*07:82', 'HLA-B*07:83', 'HLA-B*07:84', 'HLA-B*07:85', 'HLA-B*07:86', 'HLA-B*07:87', 'HLA-B*07:88',
                                     'HLA-B*07:89', 'HLA-B*07:90', 'HLA-B*07:91',
                                     'HLA-B*07:92', 'HLA-B*07:93', 'HLA-B*07:94', 'HLA-B*07:95', 'HLA-B*07:96', 'HLA-B*07:97', 'HLA-B*07:98',
                                     'HLA-B*07:99', 'HLA-B*07:100', 'HLA-B*07:101',
                                     'HLA-B*07:102', 'HLA-B*07:103', 'HLA-B*07:104', 'HLA-B*07:105', 'HLA-B*07:106', 'HLA-B*07:107', 'HLA-B*07:108',
                                     'HLA-B*07:109', 'HLA-B*07:110',
                                     'HLA-B*07:112', 'HLA-B*07:113', 'HLA-B*07:114', 'HLA-B*07:115', 'HLA-B*08:01', 'HLA-B*08:02', 'HLA-B*08:03',
                                     'HLA-B*08:04', 'HLA-B*08:05',
                                     'HLA-B*08:07', 'HLA-B*08:09', 'HLA-B*08:10', 'HLA-B*08:11', 'HLA-B*08:12', 'HLA-B*08:13', 'HLA-B*08:14',
                                     'HLA-B*08:15', 'HLA-B*08:16', 'HLA-B*08:17',
                                     'HLA-B*08:18', 'HLA-B*08:20', 'HLA-B*08:21', 'HLA-B*08:22', 'HLA-B*08:23', 'HLA-B*08:24', 'HLA-B*08:25',
                                     'HLA-B*08:26', 'HLA-B*08:27', 'HLA-B*08:28',
                                     'HLA-B*08:29', 'HLA-B*08:31', 'HLA-B*08:32', 'HLA-B*08:33', 'HLA-B*08:34', 'HLA-B*08:35', 'HLA-B*08:36',
                                     'HLA-B*08:37', 'HLA-B*08:38', 'HLA-B*08:39',
                                     'HLA-B*08:40', 'HLA-B*08:41', 'HLA-B*08:42', 'HLA-B*08:43', 'HLA-B*08:44', 'HLA-B*08:45', 'HLA-B*08:46',
                                     'HLA-B*08:47', 'HLA-B*08:48', 'HLA-B*08:49',
                                     'HLA-B*08:50', 'HLA-B*08:51', 'HLA-B*08:52', 'HLA-B*08:53', 'HLA-B*08:54', 'HLA-B*08:55', 'HLA-B*08:56',
                                     'HLA-B*08:57', 'HLA-B*08:58', 'HLA-B*08:59',
                                     'HLA-B*08:60', 'HLA-B*08:61', 'HLA-B*08:62', 'HLA-B*13:01', 'HLA-B*13:02', 'HLA-B*13:03', 'HLA-B*13:04',
                                     'HLA-B*13:06', 'HLA-B*13:09', 'HLA-B*13:10',
                                     'HLA-B*13:11', 'HLA-B*13:12', 'HLA-B*13:13', 'HLA-B*13:14', 'HLA-B*13:15', 'HLA-B*13:16', 'HLA-B*13:17',
                                     'HLA-B*13:18', 'HLA-B*13:19', 'HLA-B*13:20',
                                     'HLA-B*13:21', 'HLA-B*13:22', 'HLA-B*13:23', 'HLA-B*13:25', 'HLA-B*13:26', 'HLA-B*13:27', 'HLA-B*13:28',
                                     'HLA-B*13:29', 'HLA-B*13:30', 'HLA-B*13:31',
                                     'HLA-B*13:32', 'HLA-B*13:33', 'HLA-B*13:34', 'HLA-B*13:35', 'HLA-B*13:36', 'HLA-B*13:37', 'HLA-B*13:38',
                                     'HLA-B*13:39', 'HLA-B*14:01', 'HLA-B*14:02',
                                     'HLA-B*14:03', 'HLA-B*14:04', 'HLA-B*14:05', 'HLA-B*14:06', 'HLA-B*14:08', 'HLA-B*14:09', 'HLA-B*14:10',
                                     'HLA-B*14:11', 'HLA-B*14:12', 'HLA-B*14:13',
                                     'HLA-B*14:14', 'HLA-B*14:15', 'HLA-B*14:16', 'HLA-B*14:17', 'HLA-B*14:18', 'HLA-B*15:01', 'HLA-B*15:02',
                                     'HLA-B*15:03', 'HLA-B*15:04', 'HLA-B*15:05',
                                     'HLA-B*15:06', 'HLA-B*15:07', 'HLA-B*15:08', 'HLA-B*15:09', 'HLA-B*15:10', 'HLA-B*15:11', 'HLA-B*15:12',
                                     'HLA-B*15:13', 'HLA-B*15:14', 'HLA-B*15:15',
                                     'HLA-B*15:16', 'HLA-B*15:17', 'HLA-B*15:18', 'HLA-B*15:19', 'HLA-B*15:20', 'HLA-B*15:21', 'HLA-B*15:23',
                                     'HLA-B*15:24', 'HLA-B*15:25', 'HLA-B*15:27',
                                     'HLA-B*15:28', 'HLA-B*15:29', 'HLA-B*15:30', 'HLA-B*15:31', 'HLA-B*15:32', 'HLA-B*15:33', 'HLA-B*15:34',
                                     'HLA-B*15:35', 'HLA-B*15:36', 'HLA-B*15:37',
                                     'HLA-B*15:38', 'HLA-B*15:39', 'HLA-B*15:40', 'HLA-B*15:42', 'HLA-B*15:43', 'HLA-B*15:44', 'HLA-B*15:45',
                                     'HLA-B*15:46', 'HLA-B*15:47', 'HLA-B*15:48',
                                     'HLA-B*15:49', 'HLA-B*15:50', 'HLA-B*15:51', 'HLA-B*15:52', 'HLA-B*15:53', 'HLA-B*15:54', 'HLA-B*15:55',
                                     'HLA-B*15:56', 'HLA-B*15:57', 'HLA-B*15:58',
                                     'HLA-B*15:60', 'HLA-B*15:61', 'HLA-B*15:62', 'HLA-B*15:63', 'HLA-B*15:64', 'HLA-B*15:65', 'HLA-B*15:66',
                                     'HLA-B*15:67', 'HLA-B*15:68', 'HLA-B*15:69',
                                     'HLA-B*15:70', 'HLA-B*15:71', 'HLA-B*15:72', 'HLA-B*15:73', 'HLA-B*15:74', 'HLA-B*15:75', 'HLA-B*15:76',
                                     'HLA-B*15:77', 'HLA-B*15:78', 'HLA-B*15:80',
                                     'HLA-B*15:81', 'HLA-B*15:82', 'HLA-B*15:83', 'HLA-B*15:84', 'HLA-B*15:85', 'HLA-B*15:86', 'HLA-B*15:87',
                                     'HLA-B*15:88', 'HLA-B*15:89', 'HLA-B*15:90',
                                     'HLA-B*15:91', 'HLA-B*15:92', 'HLA-B*15:93', 'HLA-B*15:95', 'HLA-B*15:96', 'HLA-B*15:97', 'HLA-B*15:98',
                                     'HLA-B*15:99', 'HLA-B*15:101', 'HLA-B*15:102',
                                     'HLA-B*15:103', 'HLA-B*15:104', 'HLA-B*15:105', 'HLA-B*15:106', 'HLA-B*15:107', 'HLA-B*15:108', 'HLA-B*15:109',
                                     'HLA-B*15:110', 'HLA-B*15:112',
                                     'HLA-B*15:113', 'HLA-B*15:114', 'HLA-B*15:115', 'HLA-B*15:116', 'HLA-B*15:117', 'HLA-B*15:118', 'HLA-B*15:119',
                                     'HLA-B*15:120', 'HLA-B*15:121',
                                     'HLA-B*15:122', 'HLA-B*15:123', 'HLA-B*15:124', 'HLA-B*15:125', 'HLA-B*15:126', 'HLA-B*15:127', 'HLA-B*15:128',
                                     'HLA-B*15:129', 'HLA-B*15:131',
                                     'HLA-B*15:132', 'HLA-B*15:133', 'HLA-B*15:134', 'HLA-B*15:135', 'HLA-B*15:136', 'HLA-B*15:137', 'HLA-B*15:138',
                                     'HLA-B*15:139', 'HLA-B*15:140',
                                     'HLA-B*15:141', 'HLA-B*15:142', 'HLA-B*15:143', 'HLA-B*15:144', 'HLA-B*15:145', 'HLA-B*15:146', 'HLA-B*15:147',
                                     'HLA-B*15:148', 'HLA-B*15:150',
                                     'HLA-B*15:151', 'HLA-B*15:152', 'HLA-B*15:153', 'HLA-B*15:154', 'HLA-B*15:155', 'HLA-B*15:156', 'HLA-B*15:157',
                                     'HLA-B*15:158', 'HLA-B*15:159',
                                     'HLA-B*15:160', 'HLA-B*15:161', 'HLA-B*15:162', 'HLA-B*15:163', 'HLA-B*15:164', 'HLA-B*15:165', 'HLA-B*15:166',
                                     'HLA-B*15:167', 'HLA-B*15:168',
                                     'HLA-B*15:169', 'HLA-B*15:170', 'HLA-B*15:171', 'HLA-B*15:172', 'HLA-B*15:173', 'HLA-B*15:174', 'HLA-B*15:175',
                                     'HLA-B*15:176', 'HLA-B*15:177',
                                     'HLA-B*15:178', 'HLA-B*15:179', 'HLA-B*15:180', 'HLA-B*15:183', 'HLA-B*15:184', 'HLA-B*15:185', 'HLA-B*15:186',
                                     'HLA-B*15:187', 'HLA-B*15:188',
                                     'HLA-B*15:189', 'HLA-B*15:191', 'HLA-B*15:192', 'HLA-B*15:193', 'HLA-B*15:194', 'HLA-B*15:195', 'HLA-B*15:196',
                                     'HLA-B*15:197', 'HLA-B*15:198',
                                     'HLA-B*15:199', 'HLA-B*15:200', 'HLA-B*15:201', 'HLA-B*15:202', 'HLA-B*18:01', 'HLA-B*18:02', 'HLA-B*18:03',
                                     'HLA-B*18:04', 'HLA-B*18:05',
                                     'HLA-B*18:06', 'HLA-B*18:07', 'HLA-B*18:08', 'HLA-B*18:09', 'HLA-B*18:10', 'HLA-B*18:11', 'HLA-B*18:12',
                                     'HLA-B*18:13', 'HLA-B*18:14', 'HLA-B*18:15',
                                     'HLA-B*18:18', 'HLA-B*18:19', 'HLA-B*18:20', 'HLA-B*18:21', 'HLA-B*18:22', 'HLA-B*18:24', 'HLA-B*18:25',
                                     'HLA-B*18:26', 'HLA-B*18:27', 'HLA-B*18:28',
                                     'HLA-B*18:29', 'HLA-B*18:30', 'HLA-B*18:31', 'HLA-B*18:32', 'HLA-B*18:33', 'HLA-B*18:34', 'HLA-B*18:35',
                                     'HLA-B*18:36', 'HLA-B*18:37', 'HLA-B*18:38',
                                     'HLA-B*18:39', 'HLA-B*18:40', 'HLA-B*18:41', 'HLA-B*18:42', 'HLA-B*18:43', 'HLA-B*18:44', 'HLA-B*18:45',
                                     'HLA-B*18:46', 'HLA-B*18:47', 'HLA-B*18:48',
                                     'HLA-B*18:49', 'HLA-B*18:50', 'HLA-B*27:01', 'HLA-B*27:02', 'HLA-B*27:03', 'HLA-B*27:04', 'HLA-B*27:05',
                                     'HLA-B*27:06', 'HLA-B*27:07', 'HLA-B*27:08',
                                     'HLA-B*27:09', 'HLA-B*27:10', 'HLA-B*27:11', 'HLA-B*27:12', 'HLA-B*27:13', 'HLA-B*27:14', 'HLA-B*27:15',
                                     'HLA-B*27:16', 'HLA-B*27:17', 'HLA-B*27:18',
                                     'HLA-B*27:19', 'HLA-B*27:20', 'HLA-B*27:21', 'HLA-B*27:23', 'HLA-B*27:24', 'HLA-B*27:25', 'HLA-B*27:26',
                                     'HLA-B*27:27', 'HLA-B*27:28', 'HLA-B*27:29',
                                     'HLA-B*27:30', 'HLA-B*27:31', 'HLA-B*27:32', 'HLA-B*27:33', 'HLA-B*27:34', 'HLA-B*27:35', 'HLA-B*27:36',
                                     'HLA-B*27:37', 'HLA-B*27:38', 'HLA-B*27:39',
                                     'HLA-B*27:40', 'HLA-B*27:41', 'HLA-B*27:42', 'HLA-B*27:43', 'HLA-B*27:44', 'HLA-B*27:45', 'HLA-B*27:46',
                                     'HLA-B*27:47', 'HLA-B*27:48', 'HLA-B*27:49',
                                     'HLA-B*27:50', 'HLA-B*27:51', 'HLA-B*27:52', 'HLA-B*27:53', 'HLA-B*27:54', 'HLA-B*27:55', 'HLA-B*27:56',
                                     'HLA-B*27:57', 'HLA-B*27:58', 'HLA-B*27:60',
                                     'HLA-B*27:61', 'HLA-B*27:62', 'HLA-B*27:63', 'HLA-B*27:67', 'HLA-B*27:68', 'HLA-B*27:69', 'HLA-B*35:01',
                                     'HLA-B*35:02', 'HLA-B*35:03', 'HLA-B*35:04',
                                     'HLA-B*35:05', 'HLA-B*35:06', 'HLA-B*35:07', 'HLA-B*35:08', 'HLA-B*35:09', 'HLA-B*35:10', 'HLA-B*35:11',
                                     'HLA-B*35:12', 'HLA-B*35:13', 'HLA-B*35:14',
                                     'HLA-B*35:15', 'HLA-B*35:16', 'HLA-B*35:17', 'HLA-B*35:18', 'HLA-B*35:19', 'HLA-B*35:20', 'HLA-B*35:21',
                                     'HLA-B*35:22', 'HLA-B*35:23', 'HLA-B*35:24',
                                     'HLA-B*35:25', 'HLA-B*35:26', 'HLA-B*35:27', 'HLA-B*35:28', 'HLA-B*35:29', 'HLA-B*35:30', 'HLA-B*35:31',
                                     'HLA-B*35:32', 'HLA-B*35:33', 'HLA-B*35:34',
                                     'HLA-B*35:35', 'HLA-B*35:36', 'HLA-B*35:37', 'HLA-B*35:38', 'HLA-B*35:39', 'HLA-B*35:41', 'HLA-B*35:42',
                                     'HLA-B*35:43', 'HLA-B*35:44', 'HLA-B*35:45',
                                     'HLA-B*35:46', 'HLA-B*35:47', 'HLA-B*35:48', 'HLA-B*35:49', 'HLA-B*35:50', 'HLA-B*35:51', 'HLA-B*35:52',
                                     'HLA-B*35:54', 'HLA-B*35:55', 'HLA-B*35:56',
                                     'HLA-B*35:57', 'HLA-B*35:58', 'HLA-B*35:59', 'HLA-B*35:60', 'HLA-B*35:61', 'HLA-B*35:62', 'HLA-B*35:63',
                                     'HLA-B*35:64', 'HLA-B*35:66', 'HLA-B*35:67',
                                     'HLA-B*35:68', 'HLA-B*35:69', 'HLA-B*35:70', 'HLA-B*35:71', 'HLA-B*35:72', 'HLA-B*35:74', 'HLA-B*35:75',
                                     'HLA-B*35:76', 'HLA-B*35:77', 'HLA-B*35:78',
                                     'HLA-B*35:79', 'HLA-B*35:80', 'HLA-B*35:81', 'HLA-B*35:82', 'HLA-B*35:83', 'HLA-B*35:84', 'HLA-B*35:85',
                                     'HLA-B*35:86', 'HLA-B*35:87', 'HLA-B*35:88',
                                     'HLA-B*35:89', 'HLA-B*35:90', 'HLA-B*35:91', 'HLA-B*35:92', 'HLA-B*35:93', 'HLA-B*35:94', 'HLA-B*35:95',
                                     'HLA-B*35:96', 'HLA-B*35:97', 'HLA-B*35:98',
                                     'HLA-B*35:99', 'HLA-B*35:100', 'HLA-B*35:101', 'HLA-B*35:102', 'HLA-B*35:103', 'HLA-B*35:104', 'HLA-B*35:105',
                                     'HLA-B*35:106', 'HLA-B*35:107',
                                     'HLA-B*35:108', 'HLA-B*35:109', 'HLA-B*35:110', 'HLA-B*35:111', 'HLA-B*35:112', 'HLA-B*35:113', 'HLA-B*35:114',
                                     'HLA-B*35:115', 'HLA-B*35:116',
                                     'HLA-B*35:117', 'HLA-B*35:118', 'HLA-B*35:119', 'HLA-B*35:120', 'HLA-B*35:121', 'HLA-B*35:122', 'HLA-B*35:123',
                                     'HLA-B*35:124', 'HLA-B*35:125',
                                     'HLA-B*35:126', 'HLA-B*35:127', 'HLA-B*35:128', 'HLA-B*35:131', 'HLA-B*35:132', 'HLA-B*35:133', 'HLA-B*35:135',
                                     'HLA-B*35:136', 'HLA-B*35:137',
                                     'HLA-B*35:138', 'HLA-B*35:139', 'HLA-B*35:140', 'HLA-B*35:141', 'HLA-B*35:142', 'HLA-B*35:143', 'HLA-B*35:144',
                                     'HLA-B*37:01', 'HLA-B*37:02',
                                     'HLA-B*37:04', 'HLA-B*37:05', 'HLA-B*37:06', 'HLA-B*37:07', 'HLA-B*37:08', 'HLA-B*37:09', 'HLA-B*37:10',
                                     'HLA-B*37:11', 'HLA-B*37:12', 'HLA-B*37:13',
                                     'HLA-B*37:14', 'HLA-B*37:15', 'HLA-B*37:17', 'HLA-B*37:18', 'HLA-B*37:19', 'HLA-B*37:20', 'HLA-B*37:21',
                                     'HLA-B*37:22', 'HLA-B*37:23', 'HLA-B*38:01',
                                     'HLA-B*38:02', 'HLA-B*38:03', 'HLA-B*38:04', 'HLA-B*38:05', 'HLA-B*38:06', 'HLA-B*38:07', 'HLA-B*38:08',
                                     'HLA-B*38:09', 'HLA-B*38:10', 'HLA-B*38:11',
                                     'HLA-B*38:12', 'HLA-B*38:13', 'HLA-B*38:14', 'HLA-B*38:15', 'HLA-B*38:16', 'HLA-B*38:17', 'HLA-B*38:18',
                                     'HLA-B*38:19', 'HLA-B*38:20', 'HLA-B*38:21',
                                     'HLA-B*38:22', 'HLA-B*38:23', 'HLA-B*39:01', 'HLA-B*39:02', 'HLA-B*39:03', 'HLA-B*39:04', 'HLA-B*39:05',
                                     'HLA-B*39:06', 'HLA-B*39:07', 'HLA-B*39:08',
                                     'HLA-B*39:09', 'HLA-B*39:10', 'HLA-B*39:11', 'HLA-B*39:12', 'HLA-B*39:13', 'HLA-B*39:14', 'HLA-B*39:15',
                                     'HLA-B*39:16', 'HLA-B*39:17', 'HLA-B*39:18',
                                     'HLA-B*39:19', 'HLA-B*39:20', 'HLA-B*39:22', 'HLA-B*39:23', 'HLA-B*39:24', 'HLA-B*39:26', 'HLA-B*39:27',
                                     'HLA-B*39:28', 'HLA-B*39:29', 'HLA-B*39:30',
                                     'HLA-B*39:31', 'HLA-B*39:32', 'HLA-B*39:33', 'HLA-B*39:34', 'HLA-B*39:35', 'HLA-B*39:36', 'HLA-B*39:37',
                                     'HLA-B*39:39', 'HLA-B*39:41', 'HLA-B*39:42',
                                     'HLA-B*39:43', 'HLA-B*39:44', 'HLA-B*39:45', 'HLA-B*39:46', 'HLA-B*39:47', 'HLA-B*39:48', 'HLA-B*39:49',
                                     'HLA-B*39:50', 'HLA-B*39:51', 'HLA-B*39:52',
                                     'HLA-B*39:53', 'HLA-B*39:54', 'HLA-B*39:55', 'HLA-B*39:56', 'HLA-B*39:57', 'HLA-B*39:58', 'HLA-B*39:59',
                                     'HLA-B*39:60', 'HLA-B*40:01', 'HLA-B*40:02',
                                     'HLA-B*40:03', 'HLA-B*40:04', 'HLA-B*40:05', 'HLA-B*40:06', 'HLA-B*40:07', 'HLA-B*40:08', 'HLA-B*40:09',
                                     'HLA-B*40:10', 'HLA-B*40:11', 'HLA-B*40:12',
                                     'HLA-B*40:13', 'HLA-B*40:14', 'HLA-B*40:15', 'HLA-B*40:16', 'HLA-B*40:18', 'HLA-B*40:19', 'HLA-B*40:20',
                                     'HLA-B*40:21', 'HLA-B*40:23', 'HLA-B*40:24',
                                     'HLA-B*40:25', 'HLA-B*40:26', 'HLA-B*40:27', 'HLA-B*40:28', 'HLA-B*40:29', 'HLA-B*40:30', 'HLA-B*40:31',
                                     'HLA-B*40:32', 'HLA-B*40:33', 'HLA-B*40:34',
                                     'HLA-B*40:35', 'HLA-B*40:36', 'HLA-B*40:37', 'HLA-B*40:38', 'HLA-B*40:39', 'HLA-B*40:40', 'HLA-B*40:42',
                                     'HLA-B*40:43', 'HLA-B*40:44', 'HLA-B*40:45',
                                     'HLA-B*40:46', 'HLA-B*40:47', 'HLA-B*40:48', 'HLA-B*40:49', 'HLA-B*40:50', 'HLA-B*40:51', 'HLA-B*40:52',
                                     'HLA-B*40:53', 'HLA-B*40:54', 'HLA-B*40:55',
                                     'HLA-B*40:56', 'HLA-B*40:57', 'HLA-B*40:58', 'HLA-B*40:59', 'HLA-B*40:60', 'HLA-B*40:61', 'HLA-B*40:62',
                                     'HLA-B*40:63', 'HLA-B*40:64', 'HLA-B*40:65',
                                     'HLA-B*40:66', 'HLA-B*40:67', 'HLA-B*40:68', 'HLA-B*40:69', 'HLA-B*40:70', 'HLA-B*40:71', 'HLA-B*40:72',
                                     'HLA-B*40:73', 'HLA-B*40:74', 'HLA-B*40:75',
                                     'HLA-B*40:76', 'HLA-B*40:77', 'HLA-B*40:78', 'HLA-B*40:79', 'HLA-B*40:80', 'HLA-B*40:81', 'HLA-B*40:82',
                                     'HLA-B*40:83', 'HLA-B*40:84', 'HLA-B*40:85',
                                     'HLA-B*40:86', 'HLA-B*40:87', 'HLA-B*40:88', 'HLA-B*40:89', 'HLA-B*40:90', 'HLA-B*40:91', 'HLA-B*40:92',
                                     'HLA-B*40:93', 'HLA-B*40:94', 'HLA-B*40:95',
                                     'HLA-B*40:96', 'HLA-B*40:97', 'HLA-B*40:98', 'HLA-B*40:99', 'HLA-B*40:100', 'HLA-B*40:101', 'HLA-B*40:102',
                                     'HLA-B*40:103', 'HLA-B*40:104',
                                     'HLA-B*40:105', 'HLA-B*40:106', 'HLA-B*40:107', 'HLA-B*40:108', 'HLA-B*40:109', 'HLA-B*40:110', 'HLA-B*40:111',
                                     'HLA-B*40:112', 'HLA-B*40:113',
                                     'HLA-B*40:114', 'HLA-B*40:115', 'HLA-B*40:116', 'HLA-B*40:117', 'HLA-B*40:119', 'HLA-B*40:120', 'HLA-B*40:121',
                                     'HLA-B*40:122', 'HLA-B*40:123',
                                     'HLA-B*40:124', 'HLA-B*40:125', 'HLA-B*40:126', 'HLA-B*40:127', 'HLA-B*40:128', 'HLA-B*40:129', 'HLA-B*40:130',
                                     'HLA-B*40:131', 'HLA-B*40:132',
                                     'HLA-B*40:134', 'HLA-B*40:135', 'HLA-B*40:136', 'HLA-B*40:137', 'HLA-B*40:138', 'HLA-B*40:139', 'HLA-B*40:140',
                                     'HLA-B*40:141', 'HLA-B*40:143',
                                     'HLA-B*40:145', 'HLA-B*40:146', 'HLA-B*40:147', 'HLA-B*41:01', 'HLA-B*41:02', 'HLA-B*41:03', 'HLA-B*41:04',
                                     'HLA-B*41:05', 'HLA-B*41:06', 'HLA-B*41:07',
                                     'HLA-B*41:08', 'HLA-B*41:09', 'HLA-B*41:10', 'HLA-B*41:11', 'HLA-B*41:12', 'HLA-B*42:01', 'HLA-B*42:02',
                                     'HLA-B*42:04', 'HLA-B*42:05', 'HLA-B*42:06',
                                     'HLA-B*42:07', 'HLA-B*42:08', 'HLA-B*42:09', 'HLA-B*42:10', 'HLA-B*42:11', 'HLA-B*42:12', 'HLA-B*42:13',
                                     'HLA-B*42:14', 'HLA-B*44:02', 'HLA-B*44:03',
                                     'HLA-B*44:04', 'HLA-B*44:05', 'HLA-B*44:06', 'HLA-B*44:07', 'HLA-B*44:08', 'HLA-B*44:09', 'HLA-B*44:10',
                                     'HLA-B*44:11', 'HLA-B*44:12', 'HLA-B*44:13',
                                     'HLA-B*44:14', 'HLA-B*44:15', 'HLA-B*44:16', 'HLA-B*44:17', 'HLA-B*44:18', 'HLA-B*44:20', 'HLA-B*44:21',
                                     'HLA-B*44:22', 'HLA-B*44:24', 'HLA-B*44:25',
                                     'HLA-B*44:26', 'HLA-B*44:27', 'HLA-B*44:28', 'HLA-B*44:29', 'HLA-B*44:30', 'HLA-B*44:31', 'HLA-B*44:32',
                                     'HLA-B*44:33', 'HLA-B*44:34', 'HLA-B*44:35',
                                     'HLA-B*44:36', 'HLA-B*44:37', 'HLA-B*44:38', 'HLA-B*44:39', 'HLA-B*44:40', 'HLA-B*44:41', 'HLA-B*44:42',
                                     'HLA-B*44:43', 'HLA-B*44:44', 'HLA-B*44:45',
                                     'HLA-B*44:46', 'HLA-B*44:47', 'HLA-B*44:48', 'HLA-B*44:49', 'HLA-B*44:50', 'HLA-B*44:51', 'HLA-B*44:53',
                                     'HLA-B*44:54', 'HLA-B*44:55', 'HLA-B*44:57',
                                     'HLA-B*44:59', 'HLA-B*44:60', 'HLA-B*44:62', 'HLA-B*44:63', 'HLA-B*44:64', 'HLA-B*44:65', 'HLA-B*44:66',
                                     'HLA-B*44:67', 'HLA-B*44:68', 'HLA-B*44:69',
                                     'HLA-B*44:70', 'HLA-B*44:71', 'HLA-B*44:72', 'HLA-B*44:73', 'HLA-B*44:74', 'HLA-B*44:75', 'HLA-B*44:76',
                                     'HLA-B*44:77', 'HLA-B*44:78', 'HLA-B*44:79',
                                     'HLA-B*44:80', 'HLA-B*44:81', 'HLA-B*44:82', 'HLA-B*44:83', 'HLA-B*44:84', 'HLA-B*44:85', 'HLA-B*44:86',
                                     'HLA-B*44:87', 'HLA-B*44:88', 'HLA-B*44:89',
                                     'HLA-B*44:90', 'HLA-B*44:91', 'HLA-B*44:92', 'HLA-B*44:93', 'HLA-B*44:94', 'HLA-B*44:95', 'HLA-B*44:96',
                                     'HLA-B*44:97', 'HLA-B*44:98', 'HLA-B*44:99',
                                     'HLA-B*44:100', 'HLA-B*44:101', 'HLA-B*44:102', 'HLA-B*44:103', 'HLA-B*44:104', 'HLA-B*44:105', 'HLA-B*44:106',
                                     'HLA-B*44:107', 'HLA-B*44:109',
                                     'HLA-B*44:110', 'HLA-B*45:01', 'HLA-B*45:02', 'HLA-B*45:03', 'HLA-B*45:04', 'HLA-B*45:05', 'HLA-B*45:06',
                                     'HLA-B*45:07', 'HLA-B*45:08', 'HLA-B*45:09',
                                     'HLA-B*45:10', 'HLA-B*45:11', 'HLA-B*45:12', 'HLA-B*46:01', 'HLA-B*46:02', 'HLA-B*46:03', 'HLA-B*46:04',
                                     'HLA-B*46:05', 'HLA-B*46:06', 'HLA-B*46:08',
                                     'HLA-B*46:09', 'HLA-B*46:10', 'HLA-B*46:11', 'HLA-B*46:12', 'HLA-B*46:13', 'HLA-B*46:14', 'HLA-B*46:16',
                                     'HLA-B*46:17', 'HLA-B*46:18', 'HLA-B*46:19',
                                     'HLA-B*46:20', 'HLA-B*46:21', 'HLA-B*46:22', 'HLA-B*46:23', 'HLA-B*46:24', 'HLA-B*47:01', 'HLA-B*47:02',
                                     'HLA-B*47:03', 'HLA-B*47:04', 'HLA-B*47:05',
                                     'HLA-B*47:06', 'HLA-B*47:07', 'HLA-B*48:01', 'HLA-B*48:02', 'HLA-B*48:03', 'HLA-B*48:04', 'HLA-B*48:05',
                                     'HLA-B*48:06', 'HLA-B*48:07', 'HLA-B*48:08',
                                     'HLA-B*48:09', 'HLA-B*48:10', 'HLA-B*48:11', 'HLA-B*48:12', 'HLA-B*48:13', 'HLA-B*48:14', 'HLA-B*48:15',
                                     'HLA-B*48:16', 'HLA-B*48:17', 'HLA-B*48:18',
                                     'HLA-B*48:19', 'HLA-B*48:20', 'HLA-B*48:21', 'HLA-B*48:22', 'HLA-B*48:23', 'HLA-B*49:01', 'HLA-B*49:02',
                                     'HLA-B*49:03', 'HLA-B*49:04', 'HLA-B*49:05',
                                     'HLA-B*49:06', 'HLA-B*49:07', 'HLA-B*49:08', 'HLA-B*49:09', 'HLA-B*49:10', 'HLA-B*50:01', 'HLA-B*50:02',
                                     'HLA-B*50:04', 'HLA-B*50:05', 'HLA-B*50:06',
                                     'HLA-B*50:07', 'HLA-B*50:08', 'HLA-B*50:09', 'HLA-B*51:01', 'HLA-B*51:02', 'HLA-B*51:03', 'HLA-B*51:04',
                                     'HLA-B*51:05', 'HLA-B*51:06', 'HLA-B*51:07',
                                     'HLA-B*51:08', 'HLA-B*51:09', 'HLA-B*51:12', 'HLA-B*51:13', 'HLA-B*51:14', 'HLA-B*51:15', 'HLA-B*51:16',
                                     'HLA-B*51:17', 'HLA-B*51:18', 'HLA-B*51:19',
                                     'HLA-B*51:20', 'HLA-B*51:21', 'HLA-B*51:22', 'HLA-B*51:23', 'HLA-B*51:24', 'HLA-B*51:26', 'HLA-B*51:28',
                                     'HLA-B*51:29', 'HLA-B*51:30', 'HLA-B*51:31',
                                     'HLA-B*51:32', 'HLA-B*51:33', 'HLA-B*51:34', 'HLA-B*51:35', 'HLA-B*51:36', 'HLA-B*51:37', 'HLA-B*51:38',
                                     'HLA-B*51:39', 'HLA-B*51:40', 'HLA-B*51:42',
                                     'HLA-B*51:43', 'HLA-B*51:45', 'HLA-B*51:46', 'HLA-B*51:48', 'HLA-B*51:49', 'HLA-B*51:50', 'HLA-B*51:51',
                                     'HLA-B*51:52', 'HLA-B*51:53', 'HLA-B*51:54',
                                     'HLA-B*51:55', 'HLA-B*51:56', 'HLA-B*51:57', 'HLA-B*51:58', 'HLA-B*51:59', 'HLA-B*51:60', 'HLA-B*51:61',
                                     'HLA-B*51:62', 'HLA-B*51:63', 'HLA-B*51:64',
                                     'HLA-B*51:65', 'HLA-B*51:66', 'HLA-B*51:67', 'HLA-B*51:68', 'HLA-B*51:69', 'HLA-B*51:70', 'HLA-B*51:71',
                                     'HLA-B*51:72', 'HLA-B*51:73', 'HLA-B*51:74',
                                     'HLA-B*51:75', 'HLA-B*51:76', 'HLA-B*51:77', 'HLA-B*51:78', 'HLA-B*51:79', 'HLA-B*51:80', 'HLA-B*51:81',
                                     'HLA-B*51:82', 'HLA-B*51:83', 'HLA-B*51:84',
                                     'HLA-B*51:85', 'HLA-B*51:86', 'HLA-B*51:87', 'HLA-B*51:88', 'HLA-B*51:89', 'HLA-B*51:90', 'HLA-B*51:91',
                                     'HLA-B*51:92', 'HLA-B*51:93', 'HLA-B*51:94',
                                     'HLA-B*51:95', 'HLA-B*51:96', 'HLA-B*52:01', 'HLA-B*52:02', 'HLA-B*52:03', 'HLA-B*52:04', 'HLA-B*52:05',
                                     'HLA-B*52:06', 'HLA-B*52:07', 'HLA-B*52:08',
                                     'HLA-B*52:09', 'HLA-B*52:10', 'HLA-B*52:11', 'HLA-B*52:12', 'HLA-B*52:13', 'HLA-B*52:14', 'HLA-B*52:15',
                                     'HLA-B*52:16', 'HLA-B*52:17', 'HLA-B*52:18',
                                     'HLA-B*52:19', 'HLA-B*52:20', 'HLA-B*52:21', 'HLA-B*53:01', 'HLA-B*53:02', 'HLA-B*53:03', 'HLA-B*53:04',
                                     'HLA-B*53:05', 'HLA-B*53:06', 'HLA-B*53:07',
                                     'HLA-B*53:08', 'HLA-B*53:09', 'HLA-B*53:10', 'HLA-B*53:11', 'HLA-B*53:12', 'HLA-B*53:13', 'HLA-B*53:14',
                                     'HLA-B*53:15', 'HLA-B*53:16', 'HLA-B*53:17',
                                     'HLA-B*53:18', 'HLA-B*53:19', 'HLA-B*53:20', 'HLA-B*53:21', 'HLA-B*53:22', 'HLA-B*53:23', 'HLA-B*54:01',
                                     'HLA-B*54:02', 'HLA-B*54:03', 'HLA-B*54:04',
                                     'HLA-B*54:06', 'HLA-B*54:07', 'HLA-B*54:09', 'HLA-B*54:10', 'HLA-B*54:11', 'HLA-B*54:12', 'HLA-B*54:13',
                                     'HLA-B*54:14', 'HLA-B*54:15', 'HLA-B*54:16',
                                     'HLA-B*54:17', 'HLA-B*54:18', 'HLA-B*54:19', 'HLA-B*54:20', 'HLA-B*54:21', 'HLA-B*54:22', 'HLA-B*54:23',
                                     'HLA-B*55:01', 'HLA-B*55:02', 'HLA-B*55:03',
                                     'HLA-B*55:04', 'HLA-B*55:05', 'HLA-B*55:07', 'HLA-B*55:08', 'HLA-B*55:09', 'HLA-B*55:10', 'HLA-B*55:11',
                                     'HLA-B*55:12', 'HLA-B*55:13', 'HLA-B*55:14',
                                     'HLA-B*55:15', 'HLA-B*55:16', 'HLA-B*55:17', 'HLA-B*55:18', 'HLA-B*55:19', 'HLA-B*55:20', 'HLA-B*55:21',
                                     'HLA-B*55:22', 'HLA-B*55:23', 'HLA-B*55:24',
                                     'HLA-B*55:25', 'HLA-B*55:26', 'HLA-B*55:27', 'HLA-B*55:28', 'HLA-B*55:29', 'HLA-B*55:30', 'HLA-B*55:31',
                                     'HLA-B*55:32', 'HLA-B*55:33', 'HLA-B*55:34',
                                     'HLA-B*55:35', 'HLA-B*55:36', 'HLA-B*55:37', 'HLA-B*55:38', 'HLA-B*55:39', 'HLA-B*55:40', 'HLA-B*55:41',
                                     'HLA-B*55:42', 'HLA-B*55:43', 'HLA-B*56:01',
                                     'HLA-B*56:02', 'HLA-B*56:03', 'HLA-B*56:04', 'HLA-B*56:05', 'HLA-B*56:06', 'HLA-B*56:07', 'HLA-B*56:08',
                                     'HLA-B*56:09', 'HLA-B*56:10', 'HLA-B*56:11',
                                     'HLA-B*56:12', 'HLA-B*56:13', 'HLA-B*56:14', 'HLA-B*56:15', 'HLA-B*56:16', 'HLA-B*56:17', 'HLA-B*56:18',
                                     'HLA-B*56:20', 'HLA-B*56:21', 'HLA-B*56:22',
                                     'HLA-B*56:23', 'HLA-B*56:24', 'HLA-B*56:25', 'HLA-B*56:26', 'HLA-B*56:27', 'HLA-B*56:29', 'HLA-B*57:01',
                                     'HLA-B*57:02', 'HLA-B*57:03', 'HLA-B*57:04',
                                     'HLA-B*57:05', 'HLA-B*57:06', 'HLA-B*57:07', 'HLA-B*57:08', 'HLA-B*57:09', 'HLA-B*57:10', 'HLA-B*57:11',
                                     'HLA-B*57:12', 'HLA-B*57:13', 'HLA-B*57:14',
                                     'HLA-B*57:15', 'HLA-B*57:16', 'HLA-B*57:17', 'HLA-B*57:18', 'HLA-B*57:19', 'HLA-B*57:20', 'HLA-B*57:21',
                                     'HLA-B*57:22', 'HLA-B*57:23', 'HLA-B*57:24',
                                     'HLA-B*57:25', 'HLA-B*57:26', 'HLA-B*57:27', 'HLA-B*57:29', 'HLA-B*57:30', 'HLA-B*57:31', 'HLA-B*57:32',
                                     'HLA-B*58:01', 'HLA-B*58:02', 'HLA-B*58:04',
                                     'HLA-B*58:05', 'HLA-B*58:06', 'HLA-B*58:07', 'HLA-B*58:08', 'HLA-B*58:09', 'HLA-B*58:11', 'HLA-B*58:12',
                                     'HLA-B*58:13', 'HLA-B*58:14', 'HLA-B*58:15',
                                     'HLA-B*58:16', 'HLA-B*58:18', 'HLA-B*58:19', 'HLA-B*58:20', 'HLA-B*58:21', 'HLA-B*58:22', 'HLA-B*58:23',
                                     'HLA-B*58:24', 'HLA-B*58:25', 'HLA-B*58:26',
                                     'HLA-B*58:27', 'HLA-B*58:28', 'HLA-B*58:29', 'HLA-B*58:30', 'HLA-B*59:01', 'HLA-B*59:02', 'HLA-B*59:03',
                                     'HLA-B*59:04', 'HLA-B*59:05', 'HLA-B*67:01',
                                     'HLA-B*67:02', 'HLA-B*73:01', 'HLA-B*73:02', 'HLA-B*78:01', 'HLA-B*78:02', 'HLA-B*78:03', 'HLA-B*78:04',
                                     'HLA-B*78:05', 'HLA-B*78:06', 'HLA-B*78:07',
                                     'HLA-B*81:01', 'HLA-B*81:02', 'HLA-B*81:03', 'HLA-B*81:05', 'HLA-B*82:01', 'HLA-B*82:02', 'HLA-B*82:03',
                                     'HLA-B*83:01', 'HLA-C*01:02', 'HLA-C*01:03',
                                     'HLA-C*01:04', 'HLA-C*01:05', 'HLA-C*01:06', 'HLA-C*01:07', 'HLA-C*01:08', 'HLA-C*01:09', 'HLA-C*01:10',
                                     'HLA-C*01:11', 'HLA-C*01:12', 'HLA-C*01:13',
                                     'HLA-C*01:14', 'HLA-C*01:15', 'HLA-C*01:16', 'HLA-C*01:17', 'HLA-C*01:18', 'HLA-C*01:19', 'HLA-C*01:20',
                                     'HLA-C*01:21', 'HLA-C*01:22', 'HLA-C*01:23',
                                     'HLA-C*01:24', 'HLA-C*01:25', 'HLA-C*01:26', 'HLA-C*01:27', 'HLA-C*01:28', 'HLA-C*01:29', 'HLA-C*01:30',
                                     'HLA-C*01:31', 'HLA-C*01:32', 'HLA-C*01:33',
                                     'HLA-C*01:34', 'HLA-C*01:35', 'HLA-C*01:36', 'HLA-C*01:38', 'HLA-C*01:39', 'HLA-C*01:40', 'HLA-C*02:02',
                                     'HLA-C*02:03', 'HLA-C*02:04', 'HLA-C*02:05',
                                     'HLA-C*02:06', 'HLA-C*02:07', 'HLA-C*02:08', 'HLA-C*02:09', 'HLA-C*02:10', 'HLA-C*02:11', 'HLA-C*02:12',
                                     'HLA-C*02:13', 'HLA-C*02:14', 'HLA-C*02:15',
                                     'HLA-C*02:16', 'HLA-C*02:17', 'HLA-C*02:18', 'HLA-C*02:19', 'HLA-C*02:20', 'HLA-C*02:21', 'HLA-C*02:22',
                                     'HLA-C*02:23', 'HLA-C*02:24', 'HLA-C*02:26',
                                     'HLA-C*02:27', 'HLA-C*02:28', 'HLA-C*02:29', 'HLA-C*02:30', 'HLA-C*02:31', 'HLA-C*02:32', 'HLA-C*02:33',
                                     'HLA-C*02:34', 'HLA-C*02:35', 'HLA-C*02:36',
                                     'HLA-C*02:37', 'HLA-C*02:39', 'HLA-C*02:40', 'HLA-C*03:01', 'HLA-C*03:02', 'HLA-C*03:03', 'HLA-C*03:04',
                                     'HLA-C*03:05', 'HLA-C*03:06', 'HLA-C*03:07',
                                     'HLA-C*03:08', 'HLA-C*03:09', 'HLA-C*03:10', 'HLA-C*03:11', 'HLA-C*03:12', 'HLA-C*03:13', 'HLA-C*03:14',
                                     'HLA-C*03:15', 'HLA-C*03:16', 'HLA-C*03:17',
                                     'HLA-C*03:18', 'HLA-C*03:19', 'HLA-C*03:21', 'HLA-C*03:23', 'HLA-C*03:24', 'HLA-C*03:25', 'HLA-C*03:26',
                                     'HLA-C*03:27', 'HLA-C*03:28', 'HLA-C*03:29',
                                     'HLA-C*03:30', 'HLA-C*03:31', 'HLA-C*03:32', 'HLA-C*03:33', 'HLA-C*03:34', 'HLA-C*03:35', 'HLA-C*03:36',
                                     'HLA-C*03:37', 'HLA-C*03:38', 'HLA-C*03:39',
                                     'HLA-C*03:40', 'HLA-C*03:41', 'HLA-C*03:42', 'HLA-C*03:43', 'HLA-C*03:44', 'HLA-C*03:45', 'HLA-C*03:46',
                                     'HLA-C*03:47', 'HLA-C*03:48', 'HLA-C*03:49',
                                     'HLA-C*03:50', 'HLA-C*03:51', 'HLA-C*03:52', 'HLA-C*03:53', 'HLA-C*03:54', 'HLA-C*03:55', 'HLA-C*03:56',
                                     'HLA-C*03:57', 'HLA-C*03:58', 'HLA-C*03:59',
                                     'HLA-C*03:60', 'HLA-C*03:61', 'HLA-C*03:62', 'HLA-C*03:63', 'HLA-C*03:64', 'HLA-C*03:65', 'HLA-C*03:66',
                                     'HLA-C*03:67', 'HLA-C*03:68', 'HLA-C*03:69',
                                     'HLA-C*03:70', 'HLA-C*03:71', 'HLA-C*03:72', 'HLA-C*03:73', 'HLA-C*03:74', 'HLA-C*03:75', 'HLA-C*03:76',
                                     'HLA-C*03:77', 'HLA-C*03:78', 'HLA-C*03:79',
                                     'HLA-C*03:80', 'HLA-C*03:81', 'HLA-C*03:82', 'HLA-C*03:83', 'HLA-C*03:84', 'HLA-C*03:85', 'HLA-C*03:86',
                                     'HLA-C*03:87', 'HLA-C*03:88', 'HLA-C*03:89',
                                     'HLA-C*03:90', 'HLA-C*03:91', 'HLA-C*03:92', 'HLA-C*03:93', 'HLA-C*03:94', 'HLA-C*04:01', 'HLA-C*04:03',
                                     'HLA-C*04:04', 'HLA-C*04:05', 'HLA-C*04:06',
                                     'HLA-C*04:07', 'HLA-C*04:08', 'HLA-C*04:10', 'HLA-C*04:11', 'HLA-C*04:12', 'HLA-C*04:13', 'HLA-C*04:14',
                                     'HLA-C*04:15', 'HLA-C*04:16', 'HLA-C*04:17',
                                     'HLA-C*04:18', 'HLA-C*04:19', 'HLA-C*04:20', 'HLA-C*04:23', 'HLA-C*04:24', 'HLA-C*04:25', 'HLA-C*04:26',
                                     'HLA-C*04:27', 'HLA-C*04:28', 'HLA-C*04:29',
                                     'HLA-C*04:30', 'HLA-C*04:31', 'HLA-C*04:32', 'HLA-C*04:33', 'HLA-C*04:34', 'HLA-C*04:35', 'HLA-C*04:36',
                                     'HLA-C*04:37', 'HLA-C*04:38', 'HLA-C*04:39',
                                     'HLA-C*04:40', 'HLA-C*04:41', 'HLA-C*04:42', 'HLA-C*04:43', 'HLA-C*04:44', 'HLA-C*04:45', 'HLA-C*04:46',
                                     'HLA-C*04:47', 'HLA-C*04:48', 'HLA-C*04:49',
                                     'HLA-C*04:50', 'HLA-C*04:51', 'HLA-C*04:52', 'HLA-C*04:53', 'HLA-C*04:54', 'HLA-C*04:55', 'HLA-C*04:56',
                                     'HLA-C*04:57', 'HLA-C*04:58', 'HLA-C*04:60',
                                     'HLA-C*04:61', 'HLA-C*04:62', 'HLA-C*04:63', 'HLA-C*04:64', 'HLA-C*04:65', 'HLA-C*04:66', 'HLA-C*04:67',
                                     'HLA-C*04:68', 'HLA-C*04:69', 'HLA-C*04:70',
                                     'HLA-C*05:01', 'HLA-C*05:03', 'HLA-C*05:04', 'HLA-C*05:05', 'HLA-C*05:06', 'HLA-C*05:08', 'HLA-C*05:09',
                                     'HLA-C*05:10', 'HLA-C*05:11', 'HLA-C*05:12',
                                     'HLA-C*05:13', 'HLA-C*05:14', 'HLA-C*05:15', 'HLA-C*05:16', 'HLA-C*05:17', 'HLA-C*05:18', 'HLA-C*05:19',
                                     'HLA-C*05:20', 'HLA-C*05:21', 'HLA-C*05:22',
                                     'HLA-C*05:23', 'HLA-C*05:24', 'HLA-C*05:25', 'HLA-C*05:26', 'HLA-C*05:27', 'HLA-C*05:28', 'HLA-C*05:29',
                                     'HLA-C*05:30', 'HLA-C*05:31', 'HLA-C*05:32',
                                     'HLA-C*05:33', 'HLA-C*05:34', 'HLA-C*05:35', 'HLA-C*05:36', 'HLA-C*05:37', 'HLA-C*05:38', 'HLA-C*05:39',
                                     'HLA-C*05:40', 'HLA-C*05:41', 'HLA-C*05:42',
                                     'HLA-C*05:43', 'HLA-C*05:44', 'HLA-C*05:45', 'HLA-C*06:02', 'HLA-C*06:03', 'HLA-C*06:04', 'HLA-C*06:05',
                                     'HLA-C*06:06', 'HLA-C*06:07', 'HLA-C*06:08',
                                     'HLA-C*06:09', 'HLA-C*06:10', 'HLA-C*06:11', 'HLA-C*06:12', 'HLA-C*06:13', 'HLA-C*06:14', 'HLA-C*06:15',
                                     'HLA-C*06:17', 'HLA-C*06:18', 'HLA-C*06:19',
                                     'HLA-C*06:20', 'HLA-C*06:21', 'HLA-C*06:22', 'HLA-C*06:23', 'HLA-C*06:24', 'HLA-C*06:25', 'HLA-C*06:26',
                                     'HLA-C*06:27', 'HLA-C*06:28', 'HLA-C*06:29',
                                     'HLA-C*06:30', 'HLA-C*06:31', 'HLA-C*06:32', 'HLA-C*06:33', 'HLA-C*06:34', 'HLA-C*06:35', 'HLA-C*06:36',
                                     'HLA-C*06:37', 'HLA-C*06:38', 'HLA-C*06:39',
                                     'HLA-C*06:40', 'HLA-C*06:41', 'HLA-C*06:42', 'HLA-C*06:43', 'HLA-C*06:44', 'HLA-C*06:45', 'HLA-C*07:01',
                                     'HLA-C*07:02', 'HLA-C*07:03', 'HLA-C*07:04',
                                     'HLA-C*07:05', 'HLA-C*07:06', 'HLA-C*07:07', 'HLA-C*07:08', 'HLA-C*07:09', 'HLA-C*07:10', 'HLA-C*07:11',
                                     'HLA-C*07:12', 'HLA-C*07:13', 'HLA-C*07:14',
                                     'HLA-C*07:15', 'HLA-C*07:16', 'HLA-C*07:17', 'HLA-C*07:18', 'HLA-C*07:19', 'HLA-C*07:20', 'HLA-C*07:21',
                                     'HLA-C*07:22', 'HLA-C*07:23', 'HLA-C*07:24',
                                     'HLA-C*07:25', 'HLA-C*07:26', 'HLA-C*07:27', 'HLA-C*07:28', 'HLA-C*07:29', 'HLA-C*07:30', 'HLA-C*07:31',
                                     'HLA-C*07:35', 'HLA-C*07:36', 'HLA-C*07:37',
                                     'HLA-C*07:38', 'HLA-C*07:39', 'HLA-C*07:40', 'HLA-C*07:41', 'HLA-C*07:42', 'HLA-C*07:43', 'HLA-C*07:44',
                                     'HLA-C*07:45', 'HLA-C*07:46', 'HLA-C*07:47',
                                     'HLA-C*07:48', 'HLA-C*07:49', 'HLA-C*07:50', 'HLA-C*07:51', 'HLA-C*07:52', 'HLA-C*07:53', 'HLA-C*07:54',
                                     'HLA-C*07:56', 'HLA-C*07:57', 'HLA-C*07:58',
                                     'HLA-C*07:59', 'HLA-C*07:60', 'HLA-C*07:62', 'HLA-C*07:63', 'HLA-C*07:64', 'HLA-C*07:65', 'HLA-C*07:66',
                                     'HLA-C*07:67', 'HLA-C*07:68', 'HLA-C*07:69',
                                     'HLA-C*07:70', 'HLA-C*07:71', 'HLA-C*07:72', 'HLA-C*07:73', 'HLA-C*07:74', 'HLA-C*07:75', 'HLA-C*07:76',
                                     'HLA-C*07:77', 'HLA-C*07:78', 'HLA-C*07:79',
                                     'HLA-C*07:80', 'HLA-C*07:81', 'HLA-C*07:82', 'HLA-C*07:83', 'HLA-C*07:84', 'HLA-C*07:85', 'HLA-C*07:86',
                                     'HLA-C*07:87', 'HLA-C*07:88', 'HLA-C*07:89',
                                     'HLA-C*07:90', 'HLA-C*07:91', 'HLA-C*07:92', 'HLA-C*07:93', 'HLA-C*07:94', 'HLA-C*07:95', 'HLA-C*07:96',
                                     'HLA-C*07:97', 'HLA-C*07:99', 'HLA-C*07:100',
                                     'HLA-C*07:101', 'HLA-C*07:102', 'HLA-C*07:103', 'HLA-C*07:105', 'HLA-C*07:106', 'HLA-C*07:107', 'HLA-C*07:108',
                                     'HLA-C*07:109', 'HLA-C*07:110',
                                     'HLA-C*07:111', 'HLA-C*07:112', 'HLA-C*07:113', 'HLA-C*07:114', 'HLA-C*07:115', 'HLA-C*07:116', 'HLA-C*07:117',
                                     'HLA-C*07:118', 'HLA-C*07:119',
                                     'HLA-C*07:120', 'HLA-C*07:122', 'HLA-C*07:123', 'HLA-C*07:124', 'HLA-C*07:125', 'HLA-C*07:126', 'HLA-C*07:127',
                                     'HLA-C*07:128', 'HLA-C*07:129',
                                     'HLA-C*07:130', 'HLA-C*07:131', 'HLA-C*07:132', 'HLA-C*07:133', 'HLA-C*07:134', 'HLA-C*07:135', 'HLA-C*07:136',
                                     'HLA-C*07:137', 'HLA-C*07:138',
                                     'HLA-C*07:139', 'HLA-C*07:140', 'HLA-C*07:141', 'HLA-C*07:142', 'HLA-C*07:143', 'HLA-C*07:144', 'HLA-C*07:145',
                                     'HLA-C*07:146', 'HLA-C*07:147',
                                     'HLA-C*07:148', 'HLA-C*07:149', 'HLA-C*08:01', 'HLA-C*08:02', 'HLA-C*08:03', 'HLA-C*08:04', 'HLA-C*08:05',
                                     'HLA-C*08:06', 'HLA-C*08:07', 'HLA-C*08:08',
                                     'HLA-C*08:09', 'HLA-C*08:10', 'HLA-C*08:11', 'HLA-C*08:12', 'HLA-C*08:13', 'HLA-C*08:14', 'HLA-C*08:15',
                                     'HLA-C*08:16', 'HLA-C*08:17', 'HLA-C*08:18',
                                     'HLA-C*08:19', 'HLA-C*08:20', 'HLA-C*08:21', 'HLA-C*08:22', 'HLA-C*08:23', 'HLA-C*08:24', 'HLA-C*08:25',
                                     'HLA-C*08:27', 'HLA-C*08:28', 'HLA-C*08:29',
                                     'HLA-C*08:30', 'HLA-C*08:31', 'HLA-C*08:32', 'HLA-C*08:33', 'HLA-C*08:34', 'HLA-C*08:35', 'HLA-C*12:02',
                                     'HLA-C*12:03', 'HLA-C*12:04', 'HLA-C*12:05',
                                     'HLA-C*12:06', 'HLA-C*12:07', 'HLA-C*12:08', 'HLA-C*12:09', 'HLA-C*12:10', 'HLA-C*12:11', 'HLA-C*12:12',
                                     'HLA-C*12:13', 'HLA-C*12:14', 'HLA-C*12:15',
                                     'HLA-C*12:16', 'HLA-C*12:17', 'HLA-C*12:18', 'HLA-C*12:19', 'HLA-C*12:20', 'HLA-C*12:21', 'HLA-C*12:22',
                                     'HLA-C*12:23', 'HLA-C*12:24', 'HLA-C*12:25',
                                     'HLA-C*12:26', 'HLA-C*12:27', 'HLA-C*12:28', 'HLA-C*12:29', 'HLA-C*12:30', 'HLA-C*12:31', 'HLA-C*12:32',
                                     'HLA-C*12:33', 'HLA-C*12:34', 'HLA-C*12:35',
                                     'HLA-C*12:36', 'HLA-C*12:37', 'HLA-C*12:38', 'HLA-C*12:40', 'HLA-C*12:41', 'HLA-C*12:43', 'HLA-C*12:44',
                                     'HLA-C*14:02', 'HLA-C*14:03', 'HLA-C*14:04',
                                     'HLA-C*14:05', 'HLA-C*14:06', 'HLA-C*14:08', 'HLA-C*14:09', 'HLA-C*14:10', 'HLA-C*14:11', 'HLA-C*14:12',
                                     'HLA-C*14:13', 'HLA-C*14:14', 'HLA-C*14:15',
                                     'HLA-C*14:16', 'HLA-C*14:17', 'HLA-C*14:18', 'HLA-C*14:19', 'HLA-C*14:20', 'HLA-C*15:02', 'HLA-C*15:03',
                                     'HLA-C*15:04', 'HLA-C*15:05', 'HLA-C*15:06',
                                     'HLA-C*15:07', 'HLA-C*15:08', 'HLA-C*15:09', 'HLA-C*15:10', 'HLA-C*15:11', 'HLA-C*15:12', 'HLA-C*15:13',
                                     'HLA-C*15:15', 'HLA-C*15:16', 'HLA-C*15:17',
                                     'HLA-C*15:18', 'HLA-C*15:19', 'HLA-C*15:20', 'HLA-C*15:21', 'HLA-C*15:22', 'HLA-C*15:23', 'HLA-C*15:24',
                                     'HLA-C*15:25', 'HLA-C*15:26', 'HLA-C*15:27',
                                     'HLA-C*15:28', 'HLA-C*15:29', 'HLA-C*15:30', 'HLA-C*15:31', 'HLA-C*15:33', 'HLA-C*15:34', 'HLA-C*15:35',
                                     'HLA-C*16:01', 'HLA-C*16:02', 'HLA-C*16:04',
                                     'HLA-C*16:06', 'HLA-C*16:07', 'HLA-C*16:08', 'HLA-C*16:09', 'HLA-C*16:10', 'HLA-C*16:11', 'HLA-C*16:12',
                                     'HLA-C*16:13', 'HLA-C*16:14', 'HLA-C*16:15',
                                     'HLA-C*16:17', 'HLA-C*16:18', 'HLA-C*16:19', 'HLA-C*16:20', 'HLA-C*16:21', 'HLA-C*16:22', 'HLA-C*16:23',
                                     'HLA-C*16:24', 'HLA-C*16:25', 'HLA-C*16:26',
                                     'HLA-C*17:01', 'HLA-C*17:02', 'HLA-C*17:03', 'HLA-C*17:04', 'HLA-C*17:05', 'HLA-C*17:06', 'HLA-C*17:07',
                                     'HLA-C*18:01', 'HLA-C*18:02', 'HLA-C*18:03',
                                     'HLA-G*01:01', 'HLA-G*01:02', 'HLA-G*01:03', 'HLA-G*01:04', 'HLA-G*01:06', 'HLA-G*01:07', 'HLA-G*01:08',
                                     'HLA-G*01:09', 'HLA-E*01:01',
                                     'H2-Db', 'H2-Dd', 'H2-Kb', 'H2-Kd', 'H2-Kk', 'H2-Ld'])
    __version = "1.1"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    @property
    def supportedAlleles(self):
        """
        A list of supported :class:`~epytope.Core.Allele.Allele`
        """
        return self.__supported_alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s:%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        scores = defaultdict(defaultdict)
        alleles = []
        with open(file, "r") as f:
            for row in f:
                if row[0] in ["#", "-"] or row.strip() == "" or "pos" in row:
                    continue
                else:
                    allele = row.split()[HLAIndex.PICKPOCKET_1_1].replace('*','')
                    pep = row.split()[PeptideIndex.PICKPOCKET_1_1]
                    score = float(row.split()[ScoreIndex.PICKPOCKET_1_1])
                    if allele not in alleles:
                        alleles.append(allele)

                    scores[allele][pep] = score

            result = {allele: {"Score": list(scores.values())[j]} for j, allele in enumerate(alleles)}

        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`elf.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools and writes them to file in the specific format

        No return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into _file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(input))


class NetCTLpan_1_1(AExternalEpitopePrediction):
    """
    Interface for NetCTLpan 1.1.

    .. note::

        NetCTLpan - Pan-specific MHC class I epitope predictions Stranzl T., Larsen M. V., Lundegaard C., Nielsen M.
        Immunogenetics. 2010 Apr 9. [Epub ahead of print]
    """
    __name = "netctlpan"
    __command = "netCTLpan -f {peptides} -a {alleles} {options} > {out}"
    __supported_length = frozenset([8, 9, 10, 11])
    __alleles = frozenset(
        ['HLA-A*01:01', 'HLA-A*01:02', 'HLA-A*01:03', 'HLA-A*01:06', 'HLA-A*01:07', 'HLA-A*01:08', 'HLA-A*01:09', 'HLA-A*01:10', 'HLA-A*01:12',
         'HLA-A*01:13', 'HLA-A*01:14', 'HLA-A*01:17', 'HLA-A*01:19', 'HLA-A*01:20', 'HLA-A*01:21', 'HLA-A*01:23', 'HLA-A*01:24', 'HLA-A*01:25',
         'HLA-A*01:26', 'HLA-A*01:28', 'HLA-A*01:29', 'HLA-A*01:30', 'HLA-A*01:32', 'HLA-A*01:33', 'HLA-A*01:35', 'HLA-A*01:36', 'HLA-A*01:37',
         'HLA-A*01:38', 'HLA-A*01:39', 'HLA-A*01:40', 'HLA-A*01:41', 'HLA-A*01:42', 'HLA-A*01:43', 'HLA-A*01:44', 'HLA-A*01:45', 'HLA-A*01:46',
         'HLA-A*01:47', 'HLA-A*01:48', 'HLA-A*01:49', 'HLA-A*01:50', 'HLA-A*01:51', 'HLA-A*01:54', 'HLA-A*01:55', 'HLA-A*01:58', 'HLA-A*01:59',
         'HLA-A*01:60', 'HLA-A*01:61', 'HLA-A*01:62', 'HLA-A*01:63', 'HLA-A*01:64', 'HLA-A*01:65', 'HLA-A*01:66', 'HLA-A*02:01', 'HLA-A*02:02',
         'HLA-A*02:03', 'HLA-A*02:04', 'HLA-A*02:05', 'HLA-A*02:06', 'HLA-A*02:07', 'HLA-A*02:08', 'HLA-A*02:09', 'HLA-A*02:10', 'HLA-A*02:101',
         'HLA-A*02:102', 'HLA-A*02:103', 'HLA-A*02:104', 'HLA-A*02:105', 'HLA-A*02:106', 'HLA-A*02:107', 'HLA-A*02:108', 'HLA-A*02:109',
         'HLA-A*02:11', 'HLA-A*02:110', 'HLA-A*02:111', 'HLA-A*02:112', 'HLA-A*02:114', 'HLA-A*02:115', 'HLA-A*02:116', 'HLA-A*02:117',
         'HLA-A*02:118', 'HLA-A*02:119', 'HLA-A*02:12', 'HLA-A*02:120', 'HLA-A*02:121', 'HLA-A*02:122', 'HLA-A*02:123', 'HLA-A*02:124',
         'HLA-A*02:126', 'HLA-A*02:127', 'HLA-A*02:128', 'HLA-A*02:129', 'HLA-A*02:13', 'HLA-A*02:130', 'HLA-A*02:131', 'HLA-A*02:132',
         'HLA-A*02:133', 'HLA-A*02:134', 'HLA-A*02:135', 'HLA-A*02:136', 'HLA-A*02:137', 'HLA-A*02:138', 'HLA-A*02:139', 'HLA-A*02:14',
         'HLA-A*02:140', 'HLA-A*02:141', 'HLA-A*02:142', 'HLA-A*02:143', 'HLA-A*02:144', 'HLA-A*02:145', 'HLA-A*02:146', 'HLA-A*02:147',
         'HLA-A*02:148', 'HLA-A*02:149', 'HLA-A*02:150', 'HLA-A*02:151', 'HLA-A*02:152', 'HLA-A*02:153', 'HLA-A*02:154', 'HLA-A*02:155',
         'HLA-A*02:156', 'HLA-A*02:157', 'HLA-A*02:158', 'HLA-A*02:159', 'HLA-A*02:16', 'HLA-A*02:160', 'HLA-A*02:161', 'HLA-A*02:162',
         'HLA-A*02:163', 'HLA-A*02:164', 'HLA-A*02:165', 'HLA-A*02:166', 'HLA-A*02:167', 'HLA-A*02:168', 'HLA-A*02:169', 'HLA-A*02:17',
         'HLA-A*02:170', 'HLA-A*02:171', 'HLA-A*02:172', 'HLA-A*02:173', 'HLA-A*02:174', 'HLA-A*02:175', 'HLA-A*02:176', 'HLA-A*02:177',
         'HLA-A*02:178', 'HLA-A*02:179', 'HLA-A*02:18', 'HLA-A*02:180', 'HLA-A*02:181', 'HLA-A*02:182', 'HLA-A*02:183', 'HLA-A*02:184',
         'HLA-A*02:185', 'HLA-A*02:186', 'HLA-A*02:187', 'HLA-A*02:188', 'HLA-A*02:189', 'HLA-A*02:19', 'HLA-A*02:190', 'HLA-A*02:191',
         'HLA-A*02:192', 'HLA-A*02:193', 'HLA-A*02:194', 'HLA-A*02:195', 'HLA-A*02:196', 'HLA-A*02:197', 'HLA-A*02:198', 'HLA-A*02:199',
         'HLA-A*02:20', 'HLA-A*02:200', 'HLA-A*02:201', 'HLA-A*02:202', 'HLA-A*02:203', 'HLA-A*02:204', 'HLA-A*02:205', 'HLA-A*02:206',
         'HLA-A*02:207', 'HLA-A*02:208', 'HLA-A*02:209', 'HLA-A*02:21', 'HLA-A*02:210', 'HLA-A*02:211', 'HLA-A*02:212', 'HLA-A*02:213',
         'HLA-A*02:214', 'HLA-A*02:215', 'HLA-A*02:216', 'HLA-A*02:217', 'HLA-A*02:218', 'HLA-A*02:219', 'HLA-A*02:22', 'HLA-A*02:220',
         'HLA-A*02:221', 'HLA-A*02:224', 'HLA-A*02:228', 'HLA-A*02:229', 'HLA-A*02:230', 'HLA-A*02:231', 'HLA-A*02:232', 'HLA-A*02:233',
         'HLA-A*02:234', 'HLA-A*02:235', 'HLA-A*02:236', 'HLA-A*02:237', 'HLA-A*02:238', 'HLA-A*02:239', 'HLA-A*02:24', 'HLA-A*02:240',
         'HLA-A*02:241', 'HLA-A*02:242', 'HLA-A*02:243', 'HLA-A*02:244', 'HLA-A*02:245', 'HLA-A*02:246', 'HLA-A*02:247', 'HLA-A*02:248',
         'HLA-A*02:249', 'HLA-A*02:25', 'HLA-A*02:251', 'HLA-A*02:252', 'HLA-A*02:253', 'HLA-A*02:254', 'HLA-A*02:255', 'HLA-A*02:256',
         'HLA-A*02:257', 'HLA-A*02:258', 'HLA-A*02:259', 'HLA-A*02:26', 'HLA-A*02:260', 'HLA-A*02:261', 'HLA-A*02:262', 'HLA-A*02:263',
         'HLA-A*02:264', 'HLA-A*02:265', 'HLA-A*02:266', 'HLA-A*02:27', 'HLA-A*02:28', 'HLA-A*02:29', 'HLA-A*02:30', 'HLA-A*02:31', 'HLA-A*02:33',
         'HLA-A*02:34', 'HLA-A*02:35', 'HLA-A*02:36', 'HLA-A*02:37', 'HLA-A*02:38', 'HLA-A*02:39', 'HLA-A*02:40', 'HLA-A*02:41', 'HLA-A*02:42',
         'HLA-A*02:44', 'HLA-A*02:45', 'HLA-A*02:46', 'HLA-A*02:47', 'HLA-A*02:48', 'HLA-A*02:49', 'HLA-A*02:50', 'HLA-A*02:51', 'HLA-A*02:52',
         'HLA-A*02:54', 'HLA-A*02:55', 'HLA-A*02:56', 'HLA-A*02:57', 'HLA-A*02:58', 'HLA-A*02:59', 'HLA-A*02:60', 'HLA-A*02:61', 'HLA-A*02:62',
         'HLA-A*02:63', 'HLA-A*02:64', 'HLA-A*02:65', 'HLA-A*02:66', 'HLA-A*02:67', 'HLA-A*02:68', 'HLA-A*02:69', 'HLA-A*02:70', 'HLA-A*02:71',
         'HLA-A*02:72', 'HLA-A*02:73', 'HLA-A*02:74', 'HLA-A*02:75', 'HLA-A*02:76', 'HLA-A*02:77', 'HLA-A*02:78', 'HLA-A*02:79', 'HLA-A*02:80',
         'HLA-A*02:81', 'HLA-A*02:84', 'HLA-A*02:85', 'HLA-A*02:86', 'HLA-A*02:87', 'HLA-A*02:89', 'HLA-A*02:90', 'HLA-A*02:91', 'HLA-A*02:92',
         'HLA-A*02:93', 'HLA-A*02:95', 'HLA-A*02:96', 'HLA-A*02:97', 'HLA-A*02:99', 'HLA-A*03:01', 'HLA-A*03:02', 'HLA-A*03:04', 'HLA-A*03:05',
         'HLA-A*03:06', 'HLA-A*03:07', 'HLA-A*03:08', 'HLA-A*03:09', 'HLA-A*03:10', 'HLA-A*03:12', 'HLA-A*03:13', 'HLA-A*03:14', 'HLA-A*03:15',
         'HLA-A*03:16', 'HLA-A*03:17', 'HLA-A*03:18', 'HLA-A*03:19', 'HLA-A*03:20', 'HLA-A*03:22', 'HLA-A*03:23', 'HLA-A*03:24', 'HLA-A*03:25',
         'HLA-A*03:26', 'HLA-A*03:27', 'HLA-A*03:28', 'HLA-A*03:29', 'HLA-A*03:30', 'HLA-A*03:31', 'HLA-A*03:32', 'HLA-A*03:33', 'HLA-A*03:34',
         'HLA-A*03:35', 'HLA-A*03:37', 'HLA-A*03:38', 'HLA-A*03:39', 'HLA-A*03:40', 'HLA-A*03:41', 'HLA-A*03:42', 'HLA-A*03:43', 'HLA-A*03:44',
         'HLA-A*03:45', 'HLA-A*03:46', 'HLA-A*03:47', 'HLA-A*03:48', 'HLA-A*03:49', 'HLA-A*03:50', 'HLA-A*03:51', 'HLA-A*03:52', 'HLA-A*03:53',
         'HLA-A*03:54', 'HLA-A*03:55', 'HLA-A*03:56', 'HLA-A*03:57', 'HLA-A*03:58', 'HLA-A*03:59', 'HLA-A*03:60', 'HLA-A*03:61', 'HLA-A*03:62',
         'HLA-A*03:63', 'HLA-A*03:64', 'HLA-A*03:65', 'HLA-A*03:66', 'HLA-A*03:67', 'HLA-A*03:70', 'HLA-A*03:71', 'HLA-A*03:72', 'HLA-A*03:73',
         'HLA-A*03:74', 'HLA-A*03:75', 'HLA-A*03:76', 'HLA-A*03:77', 'HLA-A*03:78', 'HLA-A*03:79', 'HLA-A*03:80', 'HLA-A*03:81', 'HLA-A*03:82',
         'HLA-A*11:01', 'HLA-A*11:02', 'HLA-A*11:03', 'HLA-A*11:04', 'HLA-A*11:05', 'HLA-A*11:06', 'HLA-A*11:07', 'HLA-A*11:08', 'HLA-A*11:09',
         'HLA-A*11:10', 'HLA-A*11:11', 'HLA-A*11:12', 'HLA-A*11:13', 'HLA-A*11:14', 'HLA-A*11:15', 'HLA-A*11:16', 'HLA-A*11:17', 'HLA-A*11:18',
         'HLA-A*11:19', 'HLA-A*11:20', 'HLA-A*11:22', 'HLA-A*11:23', 'HLA-A*11:24', 'HLA-A*11:25', 'HLA-A*11:26', 'HLA-A*11:27', 'HLA-A*11:29',
         'HLA-A*11:30', 'HLA-A*11:31', 'HLA-A*11:32', 'HLA-A*11:33', 'HLA-A*11:34', 'HLA-A*11:35', 'HLA-A*11:36', 'HLA-A*11:37', 'HLA-A*11:38',
         'HLA-A*11:39', 'HLA-A*11:40', 'HLA-A*11:41', 'HLA-A*11:42', 'HLA-A*11:43', 'HLA-A*11:44', 'HLA-A*11:45', 'HLA-A*11:46', 'HLA-A*11:47',
         'HLA-A*11:48', 'HLA-A*11:49', 'HLA-A*11:51', 'HLA-A*11:53', 'HLA-A*11:54', 'HLA-A*11:55', 'HLA-A*11:56', 'HLA-A*11:57', 'HLA-A*11:58',
         'HLA-A*11:59', 'HLA-A*11:60', 'HLA-A*11:61', 'HLA-A*11:62', 'HLA-A*11:63', 'HLA-A*11:64', 'HLA-A*23:01', 'HLA-A*23:02', 'HLA-A*23:03',
         'HLA-A*23:04', 'HLA-A*23:05', 'HLA-A*23:06', 'HLA-A*23:09', 'HLA-A*23:10', 'HLA-A*23:12', 'HLA-A*23:13', 'HLA-A*23:14', 'HLA-A*23:15',
         'HLA-A*23:16', 'HLA-A*23:17', 'HLA-A*23:18', 'HLA-A*23:20', 'HLA-A*23:21', 'HLA-A*23:22', 'HLA-A*23:23', 'HLA-A*23:24', 'HLA-A*23:25',
         'HLA-A*23:26', 'HLA-A*24:02', 'HLA-A*24:03', 'HLA-A*24:04', 'HLA-A*24:05', 'HLA-A*24:06', 'HLA-A*24:07', 'HLA-A*24:08', 'HLA-A*24:10',
         'HLA-A*24:100', 'HLA-A*24:101', 'HLA-A*24:102', 'HLA-A*24:103', 'HLA-A*24:104', 'HLA-A*24:105', 'HLA-A*24:106', 'HLA-A*24:107',
         'HLA-A*24:108', 'HLA-A*24:109', 'HLA-A*24:110', 'HLA-A*24:111', 'HLA-A*24:112', 'HLA-A*24:113', 'HLA-A*24:114', 'HLA-A*24:115',
         'HLA-A*24:116', 'HLA-A*24:117', 'HLA-A*24:118', 'HLA-A*24:119', 'HLA-A*24:120', 'HLA-A*24:121', 'HLA-A*24:122', 'HLA-A*24:123',
         'HLA-A*24:124', 'HLA-A*24:125', 'HLA-A*24:126', 'HLA-A*24:127', 'HLA-A*24:128', 'HLA-A*24:129', 'HLA-A*24:13', 'HLA-A*24:130',
         'HLA-A*24:131', 'HLA-A*24:133', 'HLA-A*24:134', 'HLA-A*24:135', 'HLA-A*24:136', 'HLA-A*24:137', 'HLA-A*24:138', 'HLA-A*24:139',
         'HLA-A*24:14', 'HLA-A*24:140', 'HLA-A*24:141', 'HLA-A*24:142', 'HLA-A*24:143', 'HLA-A*24:144', 'HLA-A*24:15', 'HLA-A*24:17', 'HLA-A*24:18',
         'HLA-A*24:19', 'HLA-A*24:20', 'HLA-A*24:21', 'HLA-A*24:22', 'HLA-A*24:23', 'HLA-A*24:24', 'HLA-A*24:25', 'HLA-A*24:26', 'HLA-A*24:27',
         'HLA-A*24:28', 'HLA-A*24:29', 'HLA-A*24:30', 'HLA-A*24:31', 'HLA-A*24:32', 'HLA-A*24:33', 'HLA-A*24:34', 'HLA-A*24:35', 'HLA-A*24:37',
         'HLA-A*24:38', 'HLA-A*24:39', 'HLA-A*24:41', 'HLA-A*24:42', 'HLA-A*24:43', 'HLA-A*24:44', 'HLA-A*24:46', 'HLA-A*24:47', 'HLA-A*24:49',
         'HLA-A*24:50', 'HLA-A*24:51', 'HLA-A*24:52', 'HLA-A*24:53', 'HLA-A*24:54', 'HLA-A*24:55', 'HLA-A*24:56', 'HLA-A*24:57', 'HLA-A*24:58',
         'HLA-A*24:59', 'HLA-A*24:61', 'HLA-A*24:62', 'HLA-A*24:63', 'HLA-A*24:64', 'HLA-A*24:66', 'HLA-A*24:67', 'HLA-A*24:68', 'HLA-A*24:69',
         'HLA-A*24:70', 'HLA-A*24:71', 'HLA-A*24:72', 'HLA-A*24:73', 'HLA-A*24:74', 'HLA-A*24:75', 'HLA-A*24:76', 'HLA-A*24:77', 'HLA-A*24:78',
         'HLA-A*24:79', 'HLA-A*24:80', 'HLA-A*24:81', 'HLA-A*24:82', 'HLA-A*24:85', 'HLA-A*24:87', 'HLA-A*24:88', 'HLA-A*24:89', 'HLA-A*24:91',
         'HLA-A*24:92', 'HLA-A*24:93', 'HLA-A*24:94', 'HLA-A*24:95', 'HLA-A*24:96', 'HLA-A*24:97', 'HLA-A*24:98', 'HLA-A*24:99', 'HLA-A*25:01',
         'HLA-A*25:02', 'HLA-A*25:03', 'HLA-A*25:04', 'HLA-A*25:05', 'HLA-A*25:06', 'HLA-A*25:07', 'HLA-A*25:08', 'HLA-A*25:09', 'HLA-A*25:10',
         'HLA-A*25:11', 'HLA-A*25:13', 'HLA-A*26:01', 'HLA-A*26:02', 'HLA-A*26:03', 'HLA-A*26:04', 'HLA-A*26:05', 'HLA-A*26:06', 'HLA-A*26:07',
         'HLA-A*26:08', 'HLA-A*26:09', 'HLA-A*26:10', 'HLA-A*26:12', 'HLA-A*26:13', 'HLA-A*26:14', 'HLA-A*26:15', 'HLA-A*26:16', 'HLA-A*26:17',
         'HLA-A*26:18', 'HLA-A*26:19', 'HLA-A*26:20', 'HLA-A*26:21', 'HLA-A*26:22', 'HLA-A*26:23', 'HLA-A*26:24', 'HLA-A*26:26', 'HLA-A*26:27',
         'HLA-A*26:28', 'HLA-A*26:29', 'HLA-A*26:30', 'HLA-A*26:31', 'HLA-A*26:32', 'HLA-A*26:33', 'HLA-A*26:34', 'HLA-A*26:35', 'HLA-A*26:36',
         'HLA-A*26:37', 'HLA-A*26:38', 'HLA-A*26:39', 'HLA-A*26:40', 'HLA-A*26:41', 'HLA-A*26:42', 'HLA-A*26:43', 'HLA-A*26:45', 'HLA-A*26:46',
         'HLA-A*26:47', 'HLA-A*26:48', 'HLA-A*26:49', 'HLA-A*26:50', 'HLA-A*29:01', 'HLA-A*29:02', 'HLA-A*29:03', 'HLA-A*29:04', 'HLA-A*29:05',
         'HLA-A*29:06', 'HLA-A*29:07', 'HLA-A*29:09', 'HLA-A*29:10', 'HLA-A*29:11', 'HLA-A*29:12', 'HLA-A*29:13', 'HLA-A*29:14', 'HLA-A*29:15',
         'HLA-A*29:16', 'HLA-A*29:17', 'HLA-A*29:18', 'HLA-A*29:19', 'HLA-A*29:20', 'HLA-A*29:21', 'HLA-A*29:22', 'HLA-A*30:01', 'HLA-A*30:02',
         'HLA-A*30:03', 'HLA-A*30:04', 'HLA-A*30:06', 'HLA-A*30:07', 'HLA-A*30:08', 'HLA-A*30:09', 'HLA-A*30:10', 'HLA-A*30:11', 'HLA-A*30:12',
         'HLA-A*30:13', 'HLA-A*30:15', 'HLA-A*30:16', 'HLA-A*30:17', 'HLA-A*30:18', 'HLA-A*30:19', 'HLA-A*30:20', 'HLA-A*30:22', 'HLA-A*30:23',
         'HLA-A*30:24', 'HLA-A*30:25', 'HLA-A*30:26', 'HLA-A*30:28', 'HLA-A*30:29', 'HLA-A*30:30', 'HLA-A*30:31', 'HLA-A*30:32', 'HLA-A*30:33',
         'HLA-A*30:34', 'HLA-A*30:35', 'HLA-A*30:36', 'HLA-A*30:37', 'HLA-A*30:38', 'HLA-A*30:39', 'HLA-A*30:40', 'HLA-A*30:41', 'HLA-A*31:01',
         'HLA-A*31:02', 'HLA-A*31:03', 'HLA-A*31:04', 'HLA-A*31:05', 'HLA-A*31:06', 'HLA-A*31:07', 'HLA-A*31:08', 'HLA-A*31:09', 'HLA-A*31:10',
         'HLA-A*31:11', 'HLA-A*31:12', 'HLA-A*31:13', 'HLA-A*31:15', 'HLA-A*31:16', 'HLA-A*31:17', 'HLA-A*31:18', 'HLA-A*31:19', 'HLA-A*31:20',
         'HLA-A*31:21', 'HLA-A*31:22', 'HLA-A*31:23', 'HLA-A*31:24', 'HLA-A*31:25', 'HLA-A*31:26', 'HLA-A*31:27', 'HLA-A*31:28', 'HLA-A*31:29',
         'HLA-A*31:30', 'HLA-A*31:31', 'HLA-A*31:32', 'HLA-A*31:33', 'HLA-A*31:34', 'HLA-A*31:35', 'HLA-A*31:36', 'HLA-A*31:37', 'HLA-A*32:01',
         'HLA-A*32:02', 'HLA-A*32:03', 'HLA-A*32:04', 'HLA-A*32:05', 'HLA-A*32:06', 'HLA-A*32:07', 'HLA-A*32:08', 'HLA-A*32:09', 'HLA-A*32:10',
         'HLA-A*32:12', 'HLA-A*32:13', 'HLA-A*32:14', 'HLA-A*32:15', 'HLA-A*32:16', 'HLA-A*32:17', 'HLA-A*32:18', 'HLA-A*32:20', 'HLA-A*32:21',
         'HLA-A*32:22', 'HLA-A*32:23', 'HLA-A*32:24', 'HLA-A*32:25', 'HLA-A*33:01', 'HLA-A*33:03', 'HLA-A*33:04', 'HLA-A*33:05', 'HLA-A*33:06',
         'HLA-A*33:07', 'HLA-A*33:08', 'HLA-A*33:09', 'HLA-A*33:10', 'HLA-A*33:11', 'HLA-A*33:12', 'HLA-A*33:13', 'HLA-A*33:14', 'HLA-A*33:15',
         'HLA-A*33:16', 'HLA-A*33:17', 'HLA-A*33:18', 'HLA-A*33:19', 'HLA-A*33:20', 'HLA-A*33:21', 'HLA-A*33:22', 'HLA-A*33:23', 'HLA-A*33:24',
         'HLA-A*33:25', 'HLA-A*33:26', 'HLA-A*33:27', 'HLA-A*33:28', 'HLA-A*33:29', 'HLA-A*33:30', 'HLA-A*33:31', 'HLA-A*34:01', 'HLA-A*34:02',
         'HLA-A*34:03', 'HLA-A*34:04', 'HLA-A*34:05', 'HLA-A*34:06', 'HLA-A*34:07', 'HLA-A*34:08', 'HLA-A*36:01', 'HLA-A*36:02', 'HLA-A*36:03',
         'HLA-A*36:04', 'HLA-A*36:05', 'HLA-A*43:01', 'HLA-A*66:01', 'HLA-A*66:02', 'HLA-A*66:03', 'HLA-A*66:04', 'HLA-A*66:05', 'HLA-A*66:06',
         'HLA-A*66:07', 'HLA-A*66:08', 'HLA-A*66:09', 'HLA-A*66:10', 'HLA-A*66:11', 'HLA-A*66:12', 'HLA-A*66:13', 'HLA-A*66:14', 'HLA-A*66:15',
         'HLA-A*68:01', 'HLA-A*68:02', 'HLA-A*68:03', 'HLA-A*68:04', 'HLA-A*68:05', 'HLA-A*68:06', 'HLA-A*68:07', 'HLA-A*68:08', 'HLA-A*68:09',
         'HLA-A*68:10', 'HLA-A*68:12', 'HLA-A*68:13', 'HLA-A*68:14', 'HLA-A*68:15', 'HLA-A*68:16', 'HLA-A*68:17', 'HLA-A*68:19', 'HLA-A*68:20',
         'HLA-A*68:21', 'HLA-A*68:22', 'HLA-A*68:23', 'HLA-A*68:24', 'HLA-A*68:25', 'HLA-A*68:26', 'HLA-A*68:27', 'HLA-A*68:28', 'HLA-A*68:29',
         'HLA-A*68:30', 'HLA-A*68:31', 'HLA-A*68:32', 'HLA-A*68:33', 'HLA-A*68:34', 'HLA-A*68:35', 'HLA-A*68:36', 'HLA-A*68:37', 'HLA-A*68:38',
         'HLA-A*68:39', 'HLA-A*68:40', 'HLA-A*68:41', 'HLA-A*68:42', 'HLA-A*68:43', 'HLA-A*68:44', 'HLA-A*68:45', 'HLA-A*68:46', 'HLA-A*68:47',
         'HLA-A*68:48', 'HLA-A*68:50', 'HLA-A*68:51', 'HLA-A*68:52', 'HLA-A*68:53', 'HLA-A*68:54', 'HLA-A*69:01', 'HLA-A*74:01', 'HLA-A*74:02',
         'HLA-A*74:03', 'HLA-A*74:04', 'HLA-A*74:05', 'HLA-A*74:06', 'HLA-A*74:07', 'HLA-A*74:08', 'HLA-A*74:09', 'HLA-A*74:10', 'HLA-A*74:11',
         'HLA-A*74:13', 'HLA-A*80:01', 'HLA-A*80:02', 'HLA-B*07:02', 'HLA-B*07:03', 'HLA-B*07:04', 'HLA-B*07:05', 'HLA-B*07:06', 'HLA-B*07:07',
         'HLA-B*07:08', 'HLA-B*07:09', 'HLA-B*07:10', 'HLA-B*07:100', 'HLA-B*07:101', 'HLA-B*07:102', 'HLA-B*07:103', 'HLA-B*07:104',
         'HLA-B*07:105', 'HLA-B*07:106', 'HLA-B*07:107', 'HLA-B*07:108', 'HLA-B*07:109', 'HLA-B*07:11', 'HLA-B*07:110', 'HLA-B*07:112',
         'HLA-B*07:113', 'HLA-B*07:114', 'HLA-B*07:115', 'HLA-B*07:12', 'HLA-B*07:13', 'HLA-B*07:14', 'HLA-B*07:15', 'HLA-B*07:16', 'HLA-B*07:17',
         'HLA-B*07:18', 'HLA-B*07:19', 'HLA-B*07:20', 'HLA-B*07:21', 'HLA-B*07:22', 'HLA-B*07:23', 'HLA-B*07:24', 'HLA-B*07:25', 'HLA-B*07:26',
         'HLA-B*07:27', 'HLA-B*07:28', 'HLA-B*07:29', 'HLA-B*07:30', 'HLA-B*07:31', 'HLA-B*07:32', 'HLA-B*07:33', 'HLA-B*07:34', 'HLA-B*07:35',
         'HLA-B*07:36', 'HLA-B*07:37', 'HLA-B*07:38', 'HLA-B*07:39', 'HLA-B*07:40', 'HLA-B*07:41', 'HLA-B*07:42', 'HLA-B*07:43', 'HLA-B*07:44',
         'HLA-B*07:45', 'HLA-B*07:46', 'HLA-B*07:47', 'HLA-B*07:48', 'HLA-B*07:50', 'HLA-B*07:51', 'HLA-B*07:52', 'HLA-B*07:53', 'HLA-B*07:54',
         'HLA-B*07:55', 'HLA-B*07:56', 'HLA-B*07:57', 'HLA-B*07:58', 'HLA-B*07:59', 'HLA-B*07:60', 'HLA-B*07:61', 'HLA-B*07:62', 'HLA-B*07:63',
         'HLA-B*07:64', 'HLA-B*07:65', 'HLA-B*07:66', 'HLA-B*07:68', 'HLA-B*07:69', 'HLA-B*07:70', 'HLA-B*07:71', 'HLA-B*07:72', 'HLA-B*07:73',
         'HLA-B*07:74', 'HLA-B*07:75', 'HLA-B*07:76', 'HLA-B*07:77', 'HLA-B*07:78', 'HLA-B*07:79', 'HLA-B*07:80', 'HLA-B*07:81', 'HLA-B*07:82',
         'HLA-B*07:83', 'HLA-B*07:84', 'HLA-B*07:85', 'HLA-B*07:86', 'HLA-B*07:87', 'HLA-B*07:88', 'HLA-B*07:89', 'HLA-B*07:90', 'HLA-B*07:91',
         'HLA-B*07:92', 'HLA-B*07:93', 'HLA-B*07:94', 'HLA-B*07:95', 'HLA-B*07:96', 'HLA-B*07:97', 'HLA-B*07:98', 'HLA-B*07:99', 'HLA-B*08:01',
         'HLA-B*08:02', 'HLA-B*08:03', 'HLA-B*08:04', 'HLA-B*08:05', 'HLA-B*08:07', 'HLA-B*08:09', 'HLA-B*08:10', 'HLA-B*08:11', 'HLA-B*08:12',
         'HLA-B*08:13', 'HLA-B*08:14', 'HLA-B*08:15', 'HLA-B*08:16', 'HLA-B*08:17', 'HLA-B*08:18', 'HLA-B*08:20', 'HLA-B*08:21', 'HLA-B*08:22',
         'HLA-B*08:23', 'HLA-B*08:24', 'HLA-B*08:25', 'HLA-B*08:26', 'HLA-B*08:27', 'HLA-B*08:28', 'HLA-B*08:29', 'HLA-B*08:31', 'HLA-B*08:32',
         'HLA-B*08:33', 'HLA-B*08:34', 'HLA-B*08:35', 'HLA-B*08:36', 'HLA-B*08:37', 'HLA-B*08:38', 'HLA-B*08:39', 'HLA-B*08:40', 'HLA-B*08:41',
         'HLA-B*08:42', 'HLA-B*08:43', 'HLA-B*08:44', 'HLA-B*08:45', 'HLA-B*08:46', 'HLA-B*08:47', 'HLA-B*08:48', 'HLA-B*08:49', 'HLA-B*08:50',
         'HLA-B*08:51', 'HLA-B*08:52', 'HLA-B*08:53', 'HLA-B*08:54', 'HLA-B*08:55', 'HLA-B*08:56', 'HLA-B*08:57', 'HLA-B*08:58', 'HLA-B*08:59',
         'HLA-B*08:60', 'HLA-B*08:61', 'HLA-B*08:62', 'HLA-B*13:01', 'HLA-B*13:02', 'HLA-B*13:03', 'HLA-B*13:04', 'HLA-B*13:06', 'HLA-B*13:09',
         'HLA-B*13:10', 'HLA-B*13:11', 'HLA-B*13:12', 'HLA-B*13:13', 'HLA-B*13:14', 'HLA-B*13:15', 'HLA-B*13:16', 'HLA-B*13:17', 'HLA-B*13:18',
         'HLA-B*13:19', 'HLA-B*13:20', 'HLA-B*13:21', 'HLA-B*13:22', 'HLA-B*13:23', 'HLA-B*13:25', 'HLA-B*13:26', 'HLA-B*13:27', 'HLA-B*13:28',
         'HLA-B*13:29', 'HLA-B*13:30', 'HLA-B*13:31', 'HLA-B*13:32', 'HLA-B*13:33', 'HLA-B*13:34', 'HLA-B*13:35', 'HLA-B*13:36', 'HLA-B*13:37',
         'HLA-B*13:38', 'HLA-B*13:39', 'HLA-B*14:01', 'HLA-B*14:02', 'HLA-B*14:03', 'HLA-B*14:04', 'HLA-B*14:05', 'HLA-B*14:06', 'HLA-B*14:08',
         'HLA-B*14:09', 'HLA-B*14:10', 'HLA-B*14:11', 'HLA-B*14:12', 'HLA-B*14:13', 'HLA-B*14:14', 'HLA-B*14:15', 'HLA-B*14:16', 'HLA-B*14:17',
         'HLA-B*14:18', 'HLA-B*15:01', 'HLA-B*15:02', 'HLA-B*15:03', 'HLA-B*15:04', 'HLA-B*15:05', 'HLA-B*15:06', 'HLA-B*15:07', 'HLA-B*15:08',
         'HLA-B*15:09', 'HLA-B*15:10', 'HLA-B*15:101', 'HLA-B*15:102', 'HLA-B*15:103', 'HLA-B*15:104', 'HLA-B*15:105', 'HLA-B*15:106',
         'HLA-B*15:107', 'HLA-B*15:108', 'HLA-B*15:109', 'HLA-B*15:11', 'HLA-B*15:110', 'HLA-B*15:112', 'HLA-B*15:113', 'HLA-B*15:114',
         'HLA-B*15:115', 'HLA-B*15:116', 'HLA-B*15:117', 'HLA-B*15:118', 'HLA-B*15:119', 'HLA-B*15:12', 'HLA-B*15:120', 'HLA-B*15:121',
         'HLA-B*15:122', 'HLA-B*15:123', 'HLA-B*15:124', 'HLA-B*15:125', 'HLA-B*15:126', 'HLA-B*15:127', 'HLA-B*15:128', 'HLA-B*15:129',
         'HLA-B*15:13', 'HLA-B*15:131', 'HLA-B*15:132', 'HLA-B*15:133', 'HLA-B*15:134', 'HLA-B*15:135', 'HLA-B*15:136', 'HLA-B*15:137',
         'HLA-B*15:138', 'HLA-B*15:139', 'HLA-B*15:14', 'HLA-B*15:140', 'HLA-B*15:141', 'HLA-B*15:142', 'HLA-B*15:143', 'HLA-B*15:144',
         'HLA-B*15:145', 'HLA-B*15:146', 'HLA-B*15:147', 'HLA-B*15:148', 'HLA-B*15:15', 'HLA-B*15:150', 'HLA-B*15:151', 'HLA-B*15:152',
         'HLA-B*15:153', 'HLA-B*15:154', 'HLA-B*15:155', 'HLA-B*15:156', 'HLA-B*15:157', 'HLA-B*15:158', 'HLA-B*15:159', 'HLA-B*15:16',
         'HLA-B*15:160', 'HLA-B*15:161', 'HLA-B*15:162', 'HLA-B*15:163', 'HLA-B*15:164', 'HLA-B*15:165', 'HLA-B*15:166', 'HLA-B*15:167',
         'HLA-B*15:168', 'HLA-B*15:169', 'HLA-B*15:17', 'HLA-B*15:170', 'HLA-B*15:171', 'HLA-B*15:172', 'HLA-B*15:173', 'HLA-B*15:174',
         'HLA-B*15:175', 'HLA-B*15:176', 'HLA-B*15:177', 'HLA-B*15:178', 'HLA-B*15:179', 'HLA-B*15:18', 'HLA-B*15:180', 'HLA-B*15:183',
         'HLA-B*15:184', 'HLA-B*15:185', 'HLA-B*15:186', 'HLA-B*15:187', 'HLA-B*15:188', 'HLA-B*15:189', 'HLA-B*15:19', 'HLA-B*15:191',
         'HLA-B*15:192', 'HLA-B*15:193', 'HLA-B*15:194', 'HLA-B*15:195', 'HLA-B*15:196', 'HLA-B*15:197', 'HLA-B*15:198', 'HLA-B*15:199',
         'HLA-B*15:20', 'HLA-B*15:200', 'HLA-B*15:201', 'HLA-B*15:202', 'HLA-B*15:21', 'HLA-B*15:23', 'HLA-B*15:24', 'HLA-B*15:25', 'HLA-B*15:27',
         'HLA-B*15:28', 'HLA-B*15:29', 'HLA-B*15:30', 'HLA-B*15:31', 'HLA-B*15:32', 'HLA-B*15:33', 'HLA-B*15:34', 'HLA-B*15:35', 'HLA-B*15:36',
         'HLA-B*15:37', 'HLA-B*15:38', 'HLA-B*15:39', 'HLA-B*15:40', 'HLA-B*15:42', 'HLA-B*15:43', 'HLA-B*15:44', 'HLA-B*15:45', 'HLA-B*15:46',
         'HLA-B*15:47', 'HLA-B*15:48', 'HLA-B*15:49', 'HLA-B*15:50', 'HLA-B*15:51', 'HLA-B*15:52', 'HLA-B*15:53', 'HLA-B*15:54', 'HLA-B*15:55',
         'HLA-B*15:56', 'HLA-B*15:57', 'HLA-B*15:58', 'HLA-B*15:60', 'HLA-B*15:61', 'HLA-B*15:62', 'HLA-B*15:63', 'HLA-B*15:64', 'HLA-B*15:65',
         'HLA-B*15:66', 'HLA-B*15:67', 'HLA-B*15:68', 'HLA-B*15:69', 'HLA-B*15:70', 'HLA-B*15:71', 'HLA-B*15:72', 'HLA-B*15:73', 'HLA-B*15:74',
         'HLA-B*15:75', 'HLA-B*15:76', 'HLA-B*15:77', 'HLA-B*15:78', 'HLA-B*15:80', 'HLA-B*15:81', 'HLA-B*15:82', 'HLA-B*15:83', 'HLA-B*15:84',
         'HLA-B*15:85', 'HLA-B*15:86', 'HLA-B*15:87', 'HLA-B*15:88', 'HLA-B*15:89', 'HLA-B*15:90', 'HLA-B*15:91', 'HLA-B*15:92', 'HLA-B*15:93',
         'HLA-B*15:95', 'HLA-B*15:96', 'HLA-B*15:97', 'HLA-B*15:98', 'HLA-B*15:99', 'HLA-B*18:01', 'HLA-B*18:02', 'HLA-B*18:03', 'HLA-B*18:04',
         'HLA-B*18:05', 'HLA-B*18:06', 'HLA-B*18:07', 'HLA-B*18:08', 'HLA-B*18:09', 'HLA-B*18:10', 'HLA-B*18:11', 'HLA-B*18:12', 'HLA-B*18:13',
         'HLA-B*18:14', 'HLA-B*18:15', 'HLA-B*18:18', 'HLA-B*18:19', 'HLA-B*18:20', 'HLA-B*18:21', 'HLA-B*18:22', 'HLA-B*18:24', 'HLA-B*18:25',
         'HLA-B*18:26', 'HLA-B*18:27', 'HLA-B*18:28', 'HLA-B*18:29', 'HLA-B*18:30', 'HLA-B*18:31', 'HLA-B*18:32', 'HLA-B*18:33', 'HLA-B*18:34',
         'HLA-B*18:35', 'HLA-B*18:36', 'HLA-B*18:37', 'HLA-B*18:38', 'HLA-B*18:39', 'HLA-B*18:40', 'HLA-B*18:41', 'HLA-B*18:42', 'HLA-B*18:43',
         'HLA-B*18:44', 'HLA-B*18:45', 'HLA-B*18:46', 'HLA-B*18:47', 'HLA-B*18:48', 'HLA-B*18:49', 'HLA-B*18:50', 'HLA-B*27:01', 'HLA-B*27:02',
         'HLA-B*27:03', 'HLA-B*27:04', 'HLA-B*27:05', 'HLA-B*27:06', 'HLA-B*27:07', 'HLA-B*27:08', 'HLA-B*27:09', 'HLA-B*27:10', 'HLA-B*27:11',
         'HLA-B*27:12', 'HLA-B*27:13', 'HLA-B*27:14', 'HLA-B*27:15', 'HLA-B*27:16', 'HLA-B*27:17', 'HLA-B*27:18', 'HLA-B*27:19', 'HLA-B*27:20',
         'HLA-B*27:21', 'HLA-B*27:23', 'HLA-B*27:24', 'HLA-B*27:25', 'HLA-B*27:26', 'HLA-B*27:27', 'HLA-B*27:28', 'HLA-B*27:29', 'HLA-B*27:30',
         'HLA-B*27:31', 'HLA-B*27:32', 'HLA-B*27:33', 'HLA-B*27:34', 'HLA-B*27:35', 'HLA-B*27:36', 'HLA-B*27:37', 'HLA-B*27:38', 'HLA-B*27:39',
         'HLA-B*27:40', 'HLA-B*27:41', 'HLA-B*27:42', 'HLA-B*27:43', 'HLA-B*27:44', 'HLA-B*27:45', 'HLA-B*27:46', 'HLA-B*27:47', 'HLA-B*27:48',
         'HLA-B*27:49', 'HLA-B*27:50', 'HLA-B*27:51', 'HLA-B*27:52', 'HLA-B*27:53', 'HLA-B*27:54', 'HLA-B*27:55', 'HLA-B*27:56', 'HLA-B*27:57',
         'HLA-B*27:58', 'HLA-B*27:60', 'HLA-B*27:61', 'HLA-B*27:62', 'HLA-B*27:63', 'HLA-B*27:67', 'HLA-B*27:68', 'HLA-B*27:69', 'HLA-B*35:01',
         'HLA-B*35:02', 'HLA-B*35:03', 'HLA-B*35:04', 'HLA-B*35:05', 'HLA-B*35:06', 'HLA-B*35:07', 'HLA-B*35:08', 'HLA-B*35:09', 'HLA-B*35:10',
         'HLA-B*35:100', 'HLA-B*35:101', 'HLA-B*35:102', 'HLA-B*35:103', 'HLA-B*35:104', 'HLA-B*35:105', 'HLA-B*35:106', 'HLA-B*35:107',
         'HLA-B*35:108', 'HLA-B*35:109', 'HLA-B*35:11', 'HLA-B*35:110', 'HLA-B*35:111', 'HLA-B*35:112', 'HLA-B*35:113', 'HLA-B*35:114',
         'HLA-B*35:115', 'HLA-B*35:116', 'HLA-B*35:117', 'HLA-B*35:118', 'HLA-B*35:119', 'HLA-B*35:12', 'HLA-B*35:120', 'HLA-B*35:121',
         'HLA-B*35:122', 'HLA-B*35:123', 'HLA-B*35:124', 'HLA-B*35:125', 'HLA-B*35:126', 'HLA-B*35:127', 'HLA-B*35:128', 'HLA-B*35:13',
         'HLA-B*35:131', 'HLA-B*35:132', 'HLA-B*35:133', 'HLA-B*35:135', 'HLA-B*35:136', 'HLA-B*35:137', 'HLA-B*35:138', 'HLA-B*35:139',
         'HLA-B*35:14', 'HLA-B*35:140', 'HLA-B*35:141', 'HLA-B*35:142', 'HLA-B*35:143', 'HLA-B*35:144', 'HLA-B*35:15', 'HLA-B*35:16', 'HLA-B*35:17',
         'HLA-B*35:18', 'HLA-B*35:19', 'HLA-B*35:20', 'HLA-B*35:21', 'HLA-B*35:22', 'HLA-B*35:23', 'HLA-B*35:24', 'HLA-B*35:25', 'HLA-B*35:26',
         'HLA-B*35:27', 'HLA-B*35:28', 'HLA-B*35:29', 'HLA-B*35:30', 'HLA-B*35:31', 'HLA-B*35:32', 'HLA-B*35:33', 'HLA-B*35:34', 'HLA-B*35:35',
         'HLA-B*35:36', 'HLA-B*35:37', 'HLA-B*35:38', 'HLA-B*35:39', 'HLA-B*35:41', 'HLA-B*35:42', 'HLA-B*35:43', 'HLA-B*35:44', 'HLA-B*35:45',
         'HLA-B*35:46', 'HLA-B*35:47', 'HLA-B*35:48', 'HLA-B*35:49', 'HLA-B*35:50', 'HLA-B*35:51', 'HLA-B*35:52', 'HLA-B*35:54', 'HLA-B*35:55',
         'HLA-B*35:56', 'HLA-B*35:57', 'HLA-B*35:58', 'HLA-B*35:59', 'HLA-B*35:60', 'HLA-B*35:61', 'HLA-B*35:62', 'HLA-B*35:63', 'HLA-B*35:64',
         'HLA-B*35:66', 'HLA-B*35:67', 'HLA-B*35:68', 'HLA-B*35:69', 'HLA-B*35:70', 'HLA-B*35:71', 'HLA-B*35:72', 'HLA-B*35:74', 'HLA-B*35:75',
         'HLA-B*35:76', 'HLA-B*35:77', 'HLA-B*35:78', 'HLA-B*35:79', 'HLA-B*35:80', 'HLA-B*35:81', 'HLA-B*35:82', 'HLA-B*35:83', 'HLA-B*35:84',
         'HLA-B*35:85', 'HLA-B*35:86', 'HLA-B*35:87', 'HLA-B*35:88', 'HLA-B*35:89', 'HLA-B*35:90', 'HLA-B*35:91', 'HLA-B*35:92', 'HLA-B*35:93',
         'HLA-B*35:94', 'HLA-B*35:95', 'HLA-B*35:96', 'HLA-B*35:97', 'HLA-B*35:98', 'HLA-B*35:99', 'HLA-B*37:01', 'HLA-B*37:02', 'HLA-B*37:04',
         'HLA-B*37:05', 'HLA-B*37:06', 'HLA-B*37:07', 'HLA-B*37:08', 'HLA-B*37:09', 'HLA-B*37:10', 'HLA-B*37:11', 'HLA-B*37:12', 'HLA-B*37:13',
         'HLA-B*37:14', 'HLA-B*37:15', 'HLA-B*37:17', 'HLA-B*37:18', 'HLA-B*37:19', 'HLA-B*37:20', 'HLA-B*37:21', 'HLA-B*37:22', 'HLA-B*37:23',
         'HLA-B*38:01', 'HLA-B*38:02', 'HLA-B*38:03', 'HLA-B*38:04', 'HLA-B*38:05', 'HLA-B*38:06', 'HLA-B*38:07', 'HLA-B*38:08', 'HLA-B*38:09',
         'HLA-B*38:10', 'HLA-B*38:11', 'HLA-B*38:12', 'HLA-B*38:13', 'HLA-B*38:14', 'HLA-B*38:15', 'HLA-B*38:16', 'HLA-B*38:17', 'HLA-B*38:18',
         'HLA-B*38:19', 'HLA-B*38:20', 'HLA-B*38:21', 'HLA-B*38:22', 'HLA-B*38:23', 'HLA-B*39:01', 'HLA-B*39:02', 'HLA-B*39:03', 'HLA-B*39:04',
         'HLA-B*39:05', 'HLA-B*39:06', 'HLA-B*39:07', 'HLA-B*39:08', 'HLA-B*39:09', 'HLA-B*39:10', 'HLA-B*39:11', 'HLA-B*39:12', 'HLA-B*39:13',
         'HLA-B*39:14', 'HLA-B*39:15', 'HLA-B*39:16', 'HLA-B*39:17', 'HLA-B*39:18', 'HLA-B*39:19', 'HLA-B*39:20', 'HLA-B*39:22', 'HLA-B*39:23',
         'HLA-B*39:24', 'HLA-B*39:26', 'HLA-B*39:27', 'HLA-B*39:28', 'HLA-B*39:29', 'HLA-B*39:30', 'HLA-B*39:31', 'HLA-B*39:32', 'HLA-B*39:33',
         'HLA-B*39:34', 'HLA-B*39:35', 'HLA-B*39:36', 'HLA-B*39:37', 'HLA-B*39:39', 'HLA-B*39:41', 'HLA-B*39:42', 'HLA-B*39:43', 'HLA-B*39:44',
         'HLA-B*39:45', 'HLA-B*39:46', 'HLA-B*39:47', 'HLA-B*39:48', 'HLA-B*39:49', 'HLA-B*39:50', 'HLA-B*39:51', 'HLA-B*39:52', 'HLA-B*39:53',
         'HLA-B*39:54', 'HLA-B*39:55', 'HLA-B*39:56', 'HLA-B*39:57', 'HLA-B*39:58', 'HLA-B*39:59', 'HLA-B*39:60', 'HLA-B*40:01', 'HLA-B*40:02',
         'HLA-B*40:03', 'HLA-B*40:04', 'HLA-B*40:05', 'HLA-B*40:06', 'HLA-B*40:07', 'HLA-B*40:08', 'HLA-B*40:09', 'HLA-B*40:10', 'HLA-B*40:100',
         'HLA-B*40:101', 'HLA-B*40:102', 'HLA-B*40:103', 'HLA-B*40:104', 'HLA-B*40:105', 'HLA-B*40:106', 'HLA-B*40:107', 'HLA-B*40:108',
         'HLA-B*40:109', 'HLA-B*40:11', 'HLA-B*40:110', 'HLA-B*40:111', 'HLA-B*40:112', 'HLA-B*40:113', 'HLA-B*40:114', 'HLA-B*40:115',
         'HLA-B*40:116', 'HLA-B*40:117', 'HLA-B*40:119', 'HLA-B*40:12', 'HLA-B*40:120', 'HLA-B*40:121', 'HLA-B*40:122', 'HLA-B*40:123',
         'HLA-B*40:124', 'HLA-B*40:125', 'HLA-B*40:126', 'HLA-B*40:127', 'HLA-B*40:128', 'HLA-B*40:129', 'HLA-B*40:13', 'HLA-B*40:130',
         'HLA-B*40:131', 'HLA-B*40:132', 'HLA-B*40:134', 'HLA-B*40:135', 'HLA-B*40:136', 'HLA-B*40:137', 'HLA-B*40:138', 'HLA-B*40:139',
         'HLA-B*40:14', 'HLA-B*40:140', 'HLA-B*40:141', 'HLA-B*40:143', 'HLA-B*40:145', 'HLA-B*40:146', 'HLA-B*40:147', 'HLA-B*40:15',
         'HLA-B*40:16', 'HLA-B*40:18', 'HLA-B*40:19', 'HLA-B*40:20', 'HLA-B*40:21', 'HLA-B*40:23', 'HLA-B*40:24', 'HLA-B*40:25', 'HLA-B*40:26',
         'HLA-B*40:27', 'HLA-B*40:28', 'HLA-B*40:29', 'HLA-B*40:30', 'HLA-B*40:31', 'HLA-B*40:32', 'HLA-B*40:33', 'HLA-B*40:34', 'HLA-B*40:35',
         'HLA-B*40:36', 'HLA-B*40:37', 'HLA-B*40:38', 'HLA-B*40:39', 'HLA-B*40:40', 'HLA-B*40:42', 'HLA-B*40:43', 'HLA-B*40:44', 'HLA-B*40:45',
         'HLA-B*40:46', 'HLA-B*40:47', 'HLA-B*40:48', 'HLA-B*40:49', 'HLA-B*40:50', 'HLA-B*40:51', 'HLA-B*40:52', 'HLA-B*40:53', 'HLA-B*40:54',
         'HLA-B*40:55', 'HLA-B*40:56', 'HLA-B*40:57', 'HLA-B*40:58', 'HLA-B*40:59', 'HLA-B*40:60', 'HLA-B*40:61', 'HLA-B*40:62', 'HLA-B*40:63',
         'HLA-B*40:64', 'HLA-B*40:65', 'HLA-B*40:66', 'HLA-B*40:67', 'HLA-B*40:68', 'HLA-B*40:69', 'HLA-B*40:70', 'HLA-B*40:71', 'HLA-B*40:72',
         'HLA-B*40:73', 'HLA-B*40:74', 'HLA-B*40:75', 'HLA-B*40:76', 'HLA-B*40:77', 'HLA-B*40:78', 'HLA-B*40:79', 'HLA-B*40:80', 'HLA-B*40:81',
         'HLA-B*40:82', 'HLA-B*40:83', 'HLA-B*40:84', 'HLA-B*40:85', 'HLA-B*40:86', 'HLA-B*40:87', 'HLA-B*40:88', 'HLA-B*40:89', 'HLA-B*40:90',
         'HLA-B*40:91', 'HLA-B*40:92', 'HLA-B*40:93', 'HLA-B*40:94', 'HLA-B*40:95', 'HLA-B*40:96', 'HLA-B*40:97', 'HLA-B*40:98', 'HLA-B*40:99',
         'HLA-B*41:01', 'HLA-B*41:02', 'HLA-B*41:03', 'HLA-B*41:04', 'HLA-B*41:05', 'HLA-B*41:06', 'HLA-B*41:07', 'HLA-B*41:08', 'HLA-B*41:09',
         'HLA-B*41:10', 'HLA-B*41:11', 'HLA-B*41:12', 'HLA-B*42:01', 'HLA-B*42:02', 'HLA-B*42:04', 'HLA-B*42:05', 'HLA-B*42:06', 'HLA-B*42:07',
         'HLA-B*42:08', 'HLA-B*42:09', 'HLA-B*42:10', 'HLA-B*42:11', 'HLA-B*42:12', 'HLA-B*42:13', 'HLA-B*42:14', 'HLA-B*44:02', 'HLA-B*44:03',
         'HLA-B*44:04', 'HLA-B*44:05', 'HLA-B*44:06', 'HLA-B*44:07', 'HLA-B*44:08', 'HLA-B*44:09', 'HLA-B*44:10', 'HLA-B*44:100', 'HLA-B*44:101',
         'HLA-B*44:102', 'HLA-B*44:103', 'HLA-B*44:104', 'HLA-B*44:105', 'HLA-B*44:106', 'HLA-B*44:107', 'HLA-B*44:109', 'HLA-B*44:11',
         'HLA-B*44:110', 'HLA-B*44:12', 'HLA-B*44:13', 'HLA-B*44:14', 'HLA-B*44:15', 'HLA-B*44:16', 'HLA-B*44:17', 'HLA-B*44:18', 'HLA-B*44:20',
         'HLA-B*44:21', 'HLA-B*44:22', 'HLA-B*44:24', 'HLA-B*44:25', 'HLA-B*44:26', 'HLA-B*44:27', 'HLA-B*44:28', 'HLA-B*44:29', 'HLA-B*44:30',
         'HLA-B*44:31', 'HLA-B*44:32', 'HLA-B*44:33', 'HLA-B*44:34', 'HLA-B*44:35', 'HLA-B*44:36', 'HLA-B*44:37', 'HLA-B*44:38', 'HLA-B*44:39',
         'HLA-B*44:40', 'HLA-B*44:41', 'HLA-B*44:42', 'HLA-B*44:43', 'HLA-B*44:44', 'HLA-B*44:45', 'HLA-B*44:46', 'HLA-B*44:47', 'HLA-B*44:48',
         'HLA-B*44:49', 'HLA-B*44:50', 'HLA-B*44:51', 'HLA-B*44:53', 'HLA-B*44:54', 'HLA-B*44:55', 'HLA-B*44:57', 'HLA-B*44:59', 'HLA-B*44:60',
         'HLA-B*44:62', 'HLA-B*44:63', 'HLA-B*44:64', 'HLA-B*44:65', 'HLA-B*44:66', 'HLA-B*44:67', 'HLA-B*44:68', 'HLA-B*44:69', 'HLA-B*44:70',
         'HLA-B*44:71', 'HLA-B*44:72', 'HLA-B*44:73', 'HLA-B*44:74', 'HLA-B*44:75', 'HLA-B*44:76', 'HLA-B*44:77', 'HLA-B*44:78', 'HLA-B*44:79',
         'HLA-B*44:80', 'HLA-B*44:81', 'HLA-B*44:82', 'HLA-B*44:83', 'HLA-B*44:84', 'HLA-B*44:85', 'HLA-B*44:86', 'HLA-B*44:87', 'HLA-B*44:88',
         'HLA-B*44:89', 'HLA-B*44:90', 'HLA-B*44:91', 'HLA-B*44:92', 'HLA-B*44:93', 'HLA-B*44:94', 'HLA-B*44:95', 'HLA-B*44:96', 'HLA-B*44:97',
         'HLA-B*44:98', 'HLA-B*44:99', 'HLA-B*45:01', 'HLA-B*45:02', 'HLA-B*45:03', 'HLA-B*45:04', 'HLA-B*45:05', 'HLA-B*45:06', 'HLA-B*45:07',
         'HLA-B*45:08', 'HLA-B*45:09', 'HLA-B*45:10', 'HLA-B*45:11', 'HLA-B*45:12', 'HLA-B*46:01', 'HLA-B*46:02', 'HLA-B*46:03', 'HLA-B*46:04',
         'HLA-B*46:05', 'HLA-B*46:06', 'HLA-B*46:08', 'HLA-B*46:09', 'HLA-B*46:10', 'HLA-B*46:11', 'HLA-B*46:12', 'HLA-B*46:13', 'HLA-B*46:14',
         'HLA-B*46:16', 'HLA-B*46:17', 'HLA-B*46:18', 'HLA-B*46:19', 'HLA-B*46:20', 'HLA-B*46:21', 'HLA-B*46:22', 'HLA-B*46:23', 'HLA-B*46:24',
         'HLA-B*47:01', 'HLA-B*47:02', 'HLA-B*47:03', 'HLA-B*47:04', 'HLA-B*47:05', 'HLA-B*47:06', 'HLA-B*47:07', 'HLA-B*48:01', 'HLA-B*48:02',
         'HLA-B*48:03', 'HLA-B*48:04', 'HLA-B*48:05', 'HLA-B*48:06', 'HLA-B*48:07', 'HLA-B*48:08', 'HLA-B*48:09', 'HLA-B*48:10', 'HLA-B*48:11',
         'HLA-B*48:12', 'HLA-B*48:13', 'HLA-B*48:14', 'HLA-B*48:15', 'HLA-B*48:16', 'HLA-B*48:17', 'HLA-B*48:18', 'HLA-B*48:19', 'HLA-B*48:20',
         'HLA-B*48:21', 'HLA-B*48:22', 'HLA-B*48:23', 'HLA-B*49:01', 'HLA-B*49:02', 'HLA-B*49:03', 'HLA-B*49:04', 'HLA-B*49:05', 'HLA-B*49:06',
         'HLA-B*49:07', 'HLA-B*49:08', 'HLA-B*49:09', 'HLA-B*49:10', 'HLA-B*50:01', 'HLA-B*50:02', 'HLA-B*50:04', 'HLA-B*50:05', 'HLA-B*50:06',
         'HLA-B*50:07', 'HLA-B*50:08', 'HLA-B*50:09', 'HLA-B*51:01', 'HLA-B*51:02', 'HLA-B*51:03', 'HLA-B*51:04', 'HLA-B*51:05', 'HLA-B*51:06',
         'HLA-B*51:07', 'HLA-B*51:08', 'HLA-B*51:09', 'HLA-B*51:12', 'HLA-B*51:13', 'HLA-B*51:14', 'HLA-B*51:15', 'HLA-B*51:16', 'HLA-B*51:17',
         'HLA-B*51:18', 'HLA-B*51:19', 'HLA-B*51:20', 'HLA-B*51:21', 'HLA-B*51:22', 'HLA-B*51:23', 'HLA-B*51:24', 'HLA-B*51:26', 'HLA-B*51:28',
         'HLA-B*51:29', 'HLA-B*51:30', 'HLA-B*51:31', 'HLA-B*51:32', 'HLA-B*51:33', 'HLA-B*51:34', 'HLA-B*51:35', 'HLA-B*51:36', 'HLA-B*51:37',
         'HLA-B*51:38', 'HLA-B*51:39', 'HLA-B*51:40', 'HLA-B*51:42', 'HLA-B*51:43', 'HLA-B*51:45', 'HLA-B*51:46', 'HLA-B*51:48', 'HLA-B*51:49',
         'HLA-B*51:50', 'HLA-B*51:51', 'HLA-B*51:52', 'HLA-B*51:53', 'HLA-B*51:54', 'HLA-B*51:55', 'HLA-B*51:56', 'HLA-B*51:57', 'HLA-B*51:58',
         'HLA-B*51:59', 'HLA-B*51:60', 'HLA-B*51:61', 'HLA-B*51:62', 'HLA-B*51:63', 'HLA-B*51:64', 'HLA-B*51:65', 'HLA-B*51:66', 'HLA-B*51:67',
         'HLA-B*51:68', 'HLA-B*51:69', 'HLA-B*51:70', 'HLA-B*51:71', 'HLA-B*51:72', 'HLA-B*51:73', 'HLA-B*51:74', 'HLA-B*51:75', 'HLA-B*51:76',
         'HLA-B*51:77', 'HLA-B*51:78', 'HLA-B*51:79', 'HLA-B*51:80', 'HLA-B*51:81', 'HLA-B*51:82', 'HLA-B*51:83', 'HLA-B*51:84', 'HLA-B*51:85',
         'HLA-B*51:86', 'HLA-B*51:87', 'HLA-B*51:88', 'HLA-B*51:89', 'HLA-B*51:90', 'HLA-B*51:91', 'HLA-B*51:92', 'HLA-B*51:93', 'HLA-B*51:94',
         'HLA-B*51:95', 'HLA-B*51:96', 'HLA-B*52:01', 'HLA-B*52:02', 'HLA-B*52:03', 'HLA-B*52:04', 'HLA-B*52:05', 'HLA-B*52:06', 'HLA-B*52:07',
         'HLA-B*52:08', 'HLA-B*52:09', 'HLA-B*52:10', 'HLA-B*52:11', 'HLA-B*52:12', 'HLA-B*52:13', 'HLA-B*52:14', 'HLA-B*52:15', 'HLA-B*52:16',
         'HLA-B*52:17', 'HLA-B*52:18', 'HLA-B*52:19', 'HLA-B*52:20', 'HLA-B*52:21', 'HLA-B*53:01', 'HLA-B*53:02', 'HLA-B*53:03', 'HLA-B*53:04',
         'HLA-B*53:05', 'HLA-B*53:06', 'HLA-B*53:07', 'HLA-B*53:08', 'HLA-B*53:09', 'HLA-B*53:10', 'HLA-B*53:11', 'HLA-B*53:12', 'HLA-B*53:13',
         'HLA-B*53:14', 'HLA-B*53:15', 'HLA-B*53:16', 'HLA-B*53:17', 'HLA-B*53:18', 'HLA-B*53:19', 'HLA-B*53:20', 'HLA-B*53:21', 'HLA-B*53:22',
         'HLA-B*53:23', 'HLA-B*54:01', 'HLA-B*54:02', 'HLA-B*54:03', 'HLA-B*54:04', 'HLA-B*54:06', 'HLA-B*54:07', 'HLA-B*54:09', 'HLA-B*54:10',
         'HLA-B*54:11', 'HLA-B*54:12', 'HLA-B*54:13', 'HLA-B*54:14', 'HLA-B*54:15', 'HLA-B*54:16', 'HLA-B*54:17', 'HLA-B*54:18', 'HLA-B*54:19',
         'HLA-B*54:20', 'HLA-B*54:21', 'HLA-B*54:22', 'HLA-B*54:23', 'HLA-B*55:01', 'HLA-B*55:02', 'HLA-B*55:03', 'HLA-B*55:04', 'HLA-B*55:05',
         'HLA-B*55:07', 'HLA-B*55:08', 'HLA-B*55:09', 'HLA-B*55:10', 'HLA-B*55:11', 'HLA-B*55:12', 'HLA-B*55:13', 'HLA-B*55:14', 'HLA-B*55:15',
         'HLA-B*55:16', 'HLA-B*55:17', 'HLA-B*55:18', 'HLA-B*55:19', 'HLA-B*55:20', 'HLA-B*55:21', 'HLA-B*55:22', 'HLA-B*55:23', 'HLA-B*55:24',
         'HLA-B*55:25', 'HLA-B*55:26', 'HLA-B*55:27', 'HLA-B*55:28', 'HLA-B*55:29', 'HLA-B*55:30', 'HLA-B*55:31', 'HLA-B*55:32', 'HLA-B*55:33',
         'HLA-B*55:34', 'HLA-B*55:35', 'HLA-B*55:36', 'HLA-B*55:37', 'HLA-B*55:38', 'HLA-B*55:39', 'HLA-B*55:40', 'HLA-B*55:41', 'HLA-B*55:42',
         'HLA-B*55:43', 'HLA-B*56:01', 'HLA-B*56:02', 'HLA-B*56:03', 'HLA-B*56:04', 'HLA-B*56:05', 'HLA-B*56:06', 'HLA-B*56:07', 'HLA-B*56:08',
         'HLA-B*56:09', 'HLA-B*56:10', 'HLA-B*56:11', 'HLA-B*56:12', 'HLA-B*56:13', 'HLA-B*56:14', 'HLA-B*56:15', 'HLA-B*56:16', 'HLA-B*56:17',
         'HLA-B*56:18', 'HLA-B*56:20', 'HLA-B*56:21', 'HLA-B*56:22', 'HLA-B*56:23', 'HLA-B*56:24', 'HLA-B*56:25', 'HLA-B*56:26', 'HLA-B*56:27',
         'HLA-B*56:29', 'HLA-B*57:01', 'HLA-B*57:02', 'HLA-B*57:03', 'HLA-B*57:04', 'HLA-B*57:05', 'HLA-B*57:06', 'HLA-B*57:07', 'HLA-B*57:08',
         'HLA-B*57:09', 'HLA-B*57:10', 'HLA-B*57:11', 'HLA-B*57:12', 'HLA-B*57:13', 'HLA-B*57:14', 'HLA-B*57:15', 'HLA-B*57:16', 'HLA-B*57:17',
         'HLA-B*57:18', 'HLA-B*57:19', 'HLA-B*57:20', 'HLA-B*57:21', 'HLA-B*57:22', 'HLA-B*57:23', 'HLA-B*57:24', 'HLA-B*57:25', 'HLA-B*57:26',
         'HLA-B*57:27', 'HLA-B*57:29', 'HLA-B*57:30', 'HLA-B*57:31', 'HLA-B*57:32', 'HLA-B*58:01', 'HLA-B*58:02', 'HLA-B*58:04', 'HLA-B*58:05',
         'HLA-B*58:06', 'HLA-B*58:07', 'HLA-B*58:08', 'HLA-B*58:09', 'HLA-B*58:11', 'HLA-B*58:12', 'HLA-B*58:13', 'HLA-B*58:14', 'HLA-B*58:15',
         'HLA-B*58:16', 'HLA-B*58:18', 'HLA-B*58:19', 'HLA-B*58:20', 'HLA-B*58:21', 'HLA-B*58:22', 'HLA-B*58:23', 'HLA-B*58:24', 'HLA-B*58:25',
         'HLA-B*58:26', 'HLA-B*58:27', 'HLA-B*58:28', 'HLA-B*58:29', 'HLA-B*58:30', 'HLA-B*59:01', 'HLA-B*59:02', 'HLA-B*59:03', 'HLA-B*59:04',
         'HLA-B*59:05', 'HLA-B*67:01', 'HLA-B*67:02', 'HLA-B*73:01', 'HLA-B*73:02', 'HLA-B*78:01', 'HLA-B*78:02', 'HLA-B*78:03', 'HLA-B*78:04',
         'HLA-B*78:05', 'HLA-B*78:06', 'HLA-B*78:07', 'HLA-B*81:01', 'HLA-B*81:02', 'HLA-B*81:03', 'HLA-B*81:05', 'HLA-B*82:01', 'HLA-B*82:02',
         'HLA-B*82:03', 'HLA-B*83:01', 'HLA-C*01:02', 'HLA-C*01:03', 'HLA-C*01:04', 'HLA-C*01:05', 'HLA-C*01:06', 'HLA-C*01:07', 'HLA-C*01:08',
         'HLA-C*01:09', 'HLA-C*01:10', 'HLA-C*01:11', 'HLA-C*01:12', 'HLA-C*01:13', 'HLA-C*01:14', 'HLA-C*01:15', 'HLA-C*01:16', 'HLA-C*01:17',
         'HLA-C*01:18', 'HLA-C*01:19', 'HLA-C*01:20', 'HLA-C*01:21', 'HLA-C*01:22', 'HLA-C*01:23', 'HLA-C*01:24', 'HLA-C*01:25', 'HLA-C*01:26',
         'HLA-C*01:27', 'HLA-C*01:28', 'HLA-C*01:29', 'HLA-C*01:30', 'HLA-C*01:31', 'HLA-C*01:32', 'HLA-C*01:33', 'HLA-C*01:34', 'HLA-C*01:35',
         'HLA-C*01:36', 'HLA-C*01:38', 'HLA-C*01:39', 'HLA-C*01:40', 'HLA-C*02:02', 'HLA-C*02:03', 'HLA-C*02:04', 'HLA-C*02:05', 'HLA-C*02:06',
         'HLA-C*02:07', 'HLA-C*02:08', 'HLA-C*02:09', 'HLA-C*02:10', 'HLA-C*02:11', 'HLA-C*02:12', 'HLA-C*02:13', 'HLA-C*02:14', 'HLA-C*02:15',
         'HLA-C*02:16', 'HLA-C*02:17', 'HLA-C*02:18', 'HLA-C*02:19', 'HLA-C*02:20', 'HLA-C*02:21', 'HLA-C*02:22', 'HLA-C*02:23', 'HLA-C*02:24',
         'HLA-C*02:26', 'HLA-C*02:27', 'HLA-C*02:28', 'HLA-C*02:29', 'HLA-C*02:30', 'HLA-C*02:31', 'HLA-C*02:32', 'HLA-C*02:33', 'HLA-C*02:34',
         'HLA-C*02:35', 'HLA-C*02:36', 'HLA-C*02:37', 'HLA-C*02:39', 'HLA-C*02:40', 'HLA-C*03:01', 'HLA-C*03:02', 'HLA-C*03:03', 'HLA-C*03:04',
         'HLA-C*03:05', 'HLA-C*03:06', 'HLA-C*03:07', 'HLA-C*03:08', 'HLA-C*03:09', 'HLA-C*03:10', 'HLA-C*03:11', 'HLA-C*03:12', 'HLA-C*03:13',
         'HLA-C*03:14', 'HLA-C*03:15', 'HLA-C*03:16', 'HLA-C*03:17', 'HLA-C*03:18', 'HLA-C*03:19', 'HLA-C*03:21', 'HLA-C*03:23', 'HLA-C*03:24',
         'HLA-C*03:25', 'HLA-C*03:26', 'HLA-C*03:27', 'HLA-C*03:28', 'HLA-C*03:29', 'HLA-C*03:30', 'HLA-C*03:31', 'HLA-C*03:32', 'HLA-C*03:33',
         'HLA-C*03:34', 'HLA-C*03:35', 'HLA-C*03:36', 'HLA-C*03:37', 'HLA-C*03:38', 'HLA-C*03:39', 'HLA-C*03:40', 'HLA-C*03:41', 'HLA-C*03:42',
         'HLA-C*03:43', 'HLA-C*03:44', 'HLA-C*03:45', 'HLA-C*03:46', 'HLA-C*03:47', 'HLA-C*03:48', 'HLA-C*03:49', 'HLA-C*03:50', 'HLA-C*03:51',
         'HLA-C*03:52', 'HLA-C*03:53', 'HLA-C*03:54', 'HLA-C*03:55', 'HLA-C*03:56', 'HLA-C*03:57', 'HLA-C*03:58', 'HLA-C*03:59', 'HLA-C*03:60',
         'HLA-C*03:61', 'HLA-C*03:62', 'HLA-C*03:63', 'HLA-C*03:64', 'HLA-C*03:65', 'HLA-C*03:66', 'HLA-C*03:67', 'HLA-C*03:68', 'HLA-C*03:69',
         'HLA-C*03:70', 'HLA-C*03:71', 'HLA-C*03:72', 'HLA-C*03:73', 'HLA-C*03:74', 'HLA-C*03:75', 'HLA-C*03:76', 'HLA-C*03:77', 'HLA-C*03:78',
         'HLA-C*03:79', 'HLA-C*03:80', 'HLA-C*03:81', 'HLA-C*03:82', 'HLA-C*03:83', 'HLA-C*03:84', 'HLA-C*03:85', 'HLA-C*03:86', 'HLA-C*03:87',
         'HLA-C*03:88', 'HLA-C*03:89', 'HLA-C*03:90', 'HLA-C*03:91', 'HLA-C*03:92', 'HLA-C*03:93', 'HLA-C*03:94', 'HLA-C*04:01', 'HLA-C*04:03',
         'HLA-C*04:04', 'HLA-C*04:05', 'HLA-C*04:06', 'HLA-C*04:07', 'HLA-C*04:08', 'HLA-C*04:10', 'HLA-C*04:11', 'HLA-C*04:12', 'HLA-C*04:13',
         'HLA-C*04:14', 'HLA-C*04:15', 'HLA-C*04:16', 'HLA-C*04:17', 'HLA-C*04:18', 'HLA-C*04:19', 'HLA-C*04:20', 'HLA-C*04:23', 'HLA-C*04:24',
         'HLA-C*04:25', 'HLA-C*04:26', 'HLA-C*04:27', 'HLA-C*04:28', 'HLA-C*04:29', 'HLA-C*04:30', 'HLA-C*04:31', 'HLA-C*04:32', 'HLA-C*04:33',
         'HLA-C*04:34', 'HLA-C*04:35', 'HLA-C*04:36', 'HLA-C*04:37', 'HLA-C*04:38', 'HLA-C*04:39', 'HLA-C*04:40', 'HLA-C*04:41', 'HLA-C*04:42',
         'HLA-C*04:43', 'HLA-C*04:44', 'HLA-C*04:45', 'HLA-C*04:46', 'HLA-C*04:47', 'HLA-C*04:48', 'HLA-C*04:49', 'HLA-C*04:50', 'HLA-C*04:51',
         'HLA-C*04:52', 'HLA-C*04:53', 'HLA-C*04:54', 'HLA-C*04:55', 'HLA-C*04:56', 'HLA-C*04:57', 'HLA-C*04:58', 'HLA-C*04:60', 'HLA-C*04:61',
         'HLA-C*04:62', 'HLA-C*04:63', 'HLA-C*04:64', 'HLA-C*04:65', 'HLA-C*04:66', 'HLA-C*04:67', 'HLA-C*04:68', 'HLA-C*04:69', 'HLA-C*04:70',
         'HLA-C*05:01', 'HLA-C*05:03', 'HLA-C*05:04', 'HLA-C*05:05', 'HLA-C*05:06', 'HLA-C*05:08', 'HLA-C*05:09', 'HLA-C*05:10', 'HLA-C*05:11',
         'HLA-C*05:12', 'HLA-C*05:13', 'HLA-C*05:14', 'HLA-C*05:15', 'HLA-C*05:16', 'HLA-C*05:17', 'HLA-C*05:18', 'HLA-C*05:19', 'HLA-C*05:20',
         'HLA-C*05:21', 'HLA-C*05:22', 'HLA-C*05:23', 'HLA-C*05:24', 'HLA-C*05:25', 'HLA-C*05:26', 'HLA-C*05:27', 'HLA-C*05:28', 'HLA-C*05:29',
         'HLA-C*05:30', 'HLA-C*05:31', 'HLA-C*05:32', 'HLA-C*05:33', 'HLA-C*05:34', 'HLA-C*05:35', 'HLA-C*05:36', 'HLA-C*05:37', 'HLA-C*05:38',
         'HLA-C*05:39', 'HLA-C*05:40', 'HLA-C*05:41', 'HLA-C*05:42', 'HLA-C*05:43', 'HLA-C*05:44', 'HLA-C*05:45', 'HLA-C*06:02', 'HLA-C*06:03',
         'HLA-C*06:04', 'HLA-C*06:05', 'HLA-C*06:06', 'HLA-C*06:07', 'HLA-C*06:08', 'HLA-C*06:09', 'HLA-C*06:10', 'HLA-C*06:11', 'HLA-C*06:12',
         'HLA-C*06:13', 'HLA-C*06:14', 'HLA-C*06:15', 'HLA-C*06:17', 'HLA-C*06:18', 'HLA-C*06:19', 'HLA-C*06:20', 'HLA-C*06:21', 'HLA-C*06:22',
         'HLA-C*06:23', 'HLA-C*06:24', 'HLA-C*06:25', 'HLA-C*06:26', 'HLA-C*06:27', 'HLA-C*06:28', 'HLA-C*06:29', 'HLA-C*06:30', 'HLA-C*06:31',
         'HLA-C*06:32', 'HLA-C*06:33', 'HLA-C*06:34', 'HLA-C*06:35', 'HLA-C*06:36', 'HLA-C*06:37', 'HLA-C*06:38', 'HLA-C*06:39', 'HLA-C*06:40',
         'HLA-C*06:41', 'HLA-C*06:42', 'HLA-C*06:43', 'HLA-C*06:44', 'HLA-C*06:45', 'HLA-C*07:01', 'HLA-C*07:02', 'HLA-C*07:03', 'HLA-C*07:04',
         'HLA-C*07:05', 'HLA-C*07:06', 'HLA-C*07:07', 'HLA-C*07:08', 'HLA-C*07:09', 'HLA-C*07:10', 'HLA-C*07:100', 'HLA-C*07:101', 'HLA-C*07:102',
         'HLA-C*07:103', 'HLA-C*07:105', 'HLA-C*07:106', 'HLA-C*07:107', 'HLA-C*07:108', 'HLA-C*07:109', 'HLA-C*07:11', 'HLA-C*07:110',
         'HLA-C*07:111', 'HLA-C*07:112', 'HLA-C*07:113', 'HLA-C*07:114', 'HLA-C*07:115', 'HLA-C*07:116', 'HLA-C*07:117', 'HLA-C*07:118',
         'HLA-C*07:119', 'HLA-C*07:12', 'HLA-C*07:120', 'HLA-C*07:122', 'HLA-C*07:123', 'HLA-C*07:124', 'HLA-C*07:125', 'HLA-C*07:126',
         'HLA-C*07:127', 'HLA-C*07:128', 'HLA-C*07:129', 'HLA-C*07:13', 'HLA-C*07:130', 'HLA-C*07:131', 'HLA-C*07:132', 'HLA-C*07:133',
         'HLA-C*07:134', 'HLA-C*07:135', 'HLA-C*07:136', 'HLA-C*07:137', 'HLA-C*07:138', 'HLA-C*07:139', 'HLA-C*07:14', 'HLA-C*07:140',
         'HLA-C*07:141', 'HLA-C*07:142', 'HLA-C*07:143', 'HLA-C*07:144', 'HLA-C*07:145', 'HLA-C*07:146', 'HLA-C*07:147', 'HLA-C*07:148',
         'HLA-C*07:149', 'HLA-C*07:15', 'HLA-C*07:16', 'HLA-C*07:17', 'HLA-C*07:18', 'HLA-C*07:19', 'HLA-C*07:20', 'HLA-C*07:21', 'HLA-C*07:22',
         'HLA-C*07:23', 'HLA-C*07:24', 'HLA-C*07:25', 'HLA-C*07:26', 'HLA-C*07:27', 'HLA-C*07:28', 'HLA-C*07:29', 'HLA-C*07:30', 'HLA-C*07:31',
         'HLA-C*07:35', 'HLA-C*07:36', 'HLA-C*07:37', 'HLA-C*07:38', 'HLA-C*07:39', 'HLA-C*07:40', 'HLA-C*07:41', 'HLA-C*07:42', 'HLA-C*07:43',
         'HLA-C*07:44', 'HLA-C*07:45', 'HLA-C*07:46', 'HLA-C*07:47', 'HLA-C*07:48', 'HLA-C*07:49', 'HLA-C*07:50', 'HLA-C*07:51', 'HLA-C*07:52',
         'HLA-C*07:53', 'HLA-C*07:54', 'HLA-C*07:56', 'HLA-C*07:57', 'HLA-C*07:58', 'HLA-C*07:59', 'HLA-C*07:60', 'HLA-C*07:62', 'HLA-C*07:63',
         'HLA-C*07:64', 'HLA-C*07:65', 'HLA-C*07:66', 'HLA-C*07:67', 'HLA-C*07:68', 'HLA-C*07:69', 'HLA-C*07:70', 'HLA-C*07:71', 'HLA-C*07:72',
         'HLA-C*07:73', 'HLA-C*07:74', 'HLA-C*07:75', 'HLA-C*07:76', 'HLA-C*07:77', 'HLA-C*07:78', 'HLA-C*07:79', 'HLA-C*07:80', 'HLA-C*07:81',
         'HLA-C*07:82', 'HLA-C*07:83', 'HLA-C*07:84', 'HLA-C*07:85', 'HLA-C*07:86', 'HLA-C*07:87', 'HLA-C*07:88', 'HLA-C*07:89', 'HLA-C*07:90',
         'HLA-C*07:91', 'HLA-C*07:92', 'HLA-C*07:93', 'HLA-C*07:94', 'HLA-C*07:95', 'HLA-C*07:96', 'HLA-C*07:97', 'HLA-C*07:99', 'HLA-C*08:01',
         'HLA-C*08:02', 'HLA-C*08:03', 'HLA-C*08:04', 'HLA-C*08:05', 'HLA-C*08:06', 'HLA-C*08:07', 'HLA-C*08:08', 'HLA-C*08:09', 'HLA-C*08:10',
         'HLA-C*08:11', 'HLA-C*08:12', 'HLA-C*08:13', 'HLA-C*08:14', 'HLA-C*08:15', 'HLA-C*08:16', 'HLA-C*08:17', 'HLA-C*08:18', 'HLA-C*08:19',
         'HLA-C*08:20', 'HLA-C*08:21', 'HLA-C*08:22', 'HLA-C*08:23', 'HLA-C*08:24', 'HLA-C*08:25', 'HLA-C*08:27', 'HLA-C*08:28', 'HLA-C*08:29',
         'HLA-C*08:30', 'HLA-C*08:31', 'HLA-C*08:32', 'HLA-C*08:33', 'HLA-C*08:34', 'HLA-C*08:35', 'HLA-C*12:02', 'HLA-C*12:03', 'HLA-C*12:04',
         'HLA-C*12:05', 'HLA-C*12:06', 'HLA-C*12:07', 'HLA-C*12:08', 'HLA-C*12:09', 'HLA-C*12:10', 'HLA-C*12:11', 'HLA-C*12:12', 'HLA-C*12:13',
         'HLA-C*12:14', 'HLA-C*12:15', 'HLA-C*12:16', 'HLA-C*12:17', 'HLA-C*12:18', 'HLA-C*12:19', 'HLA-C*12:20', 'HLA-C*12:21', 'HLA-C*12:22',
         'HLA-C*12:23', 'HLA-C*12:24', 'HLA-C*12:25', 'HLA-C*12:26', 'HLA-C*12:27', 'HLA-C*12:28', 'HLA-C*12:29', 'HLA-C*12:30', 'HLA-C*12:31',
         'HLA-C*12:32', 'HLA-C*12:33', 'HLA-C*12:34', 'HLA-C*12:35', 'HLA-C*12:36', 'HLA-C*12:37', 'HLA-C*12:38', 'HLA-C*12:40', 'HLA-C*12:41',
         'HLA-C*12:43', 'HLA-C*12:44', 'HLA-C*14:02', 'HLA-C*14:03', 'HLA-C*14:04', 'HLA-C*14:05', 'HLA-C*14:06', 'HLA-C*14:08', 'HLA-C*14:09',
         'HLA-C*14:10', 'HLA-C*14:11', 'HLA-C*14:12', 'HLA-C*14:13', 'HLA-C*14:14', 'HLA-C*14:15', 'HLA-C*14:16', 'HLA-C*14:17', 'HLA-C*14:18',
         'HLA-C*14:19', 'HLA-C*14:20', 'HLA-C*15:02', 'HLA-C*15:03', 'HLA-C*15:04', 'HLA-C*15:05', 'HLA-C*15:06', 'HLA-C*15:07', 'HLA-C*15:08',
         'HLA-C*15:09', 'HLA-C*15:10', 'HLA-C*15:11', 'HLA-C*15:12', 'HLA-C*15:13', 'HLA-C*15:15', 'HLA-C*15:16', 'HLA-C*15:17', 'HLA-C*15:18',
         'HLA-C*15:19', 'HLA-C*15:20', 'HLA-C*15:21', 'HLA-C*15:22', 'HLA-C*15:23', 'HLA-C*15:24', 'HLA-C*15:25', 'HLA-C*15:26', 'HLA-C*15:27',
         'HLA-C*15:28', 'HLA-C*15:29', 'HLA-C*15:30', 'HLA-C*15:31', 'HLA-C*15:33', 'HLA-C*15:34', 'HLA-C*15:35', 'HLA-C*16:01', 'HLA-C*16:02',
         'HLA-C*16:04', 'HLA-C*16:06', 'HLA-C*16:07', 'HLA-C*16:08', 'HLA-C*16:09', 'HLA-C*16:10', 'HLA-C*16:11', 'HLA-C*16:12', 'HLA-C*16:13',
         'HLA-C*16:14', 'HLA-C*16:15', 'HLA-C*16:17', 'HLA-C*16:18', 'HLA-C*16:19', 'HLA-C*16:20', 'HLA-C*16:21', 'HLA-C*16:22', 'HLA-C*16:23',
         'HLA-C*16:24', 'HLA-C*16:25', 'HLA-C*16:26', 'HLA-C*17:01', 'HLA-C*17:02', 'HLA-C*17:03', 'HLA-C*17:04', 'HLA-C*17:05', 'HLA-C*17:06',
         'HLA-C*17:07', 'HLA-C*18:01', 'HLA-C*18:02', 'HLA-C*18:03', 'HLA-E*01:01', 'HLA-G*01:01', 'HLA-G*01:02', 'HLA-G*01:03', 'HLA-G*01:04',
         'HLA-G*01:06', 'HLA-G*01:07', 'HLA-G*01:08', 'HLA-G*01:09',
         'H2-Db', 'H2-Dd', 'H2-Kb', 'H2-Kd', 'H2-Kk', 'H2-Ld'])
    __version = "1.1"

    @property
    def version(self):
        """The version of the predictor"""
        return self.__version

    def _represent(self, allele):
        """
        Internal function transforming an allele object into its representative string
        :param allele: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: str
        """
        if isinstance(allele, MouseAllele):
            return "H-2-%s%s%s" % (allele.locus, allele.supertype, allele.subtype)
        else:
            return "HLA-%s%s:%s" % (allele.locus, allele.supertype, allele.subtype)

    def convert_alleles(self, alleles):
        """
        Converts :class:`~epytope.Core.Allele.Allele` into the internal :class:`~epytope.Core.Allele.Allele` representation
        of the predictor and returns a string representation

        :param alleles: The :class:`~epytope.Core.Allele.Allele` for which the internal predictor representation is
                        needed
        :type alleles: :class:`~epytope.Core.Allele.Allele`
        :return: Returns a string representation of the input :class:`~epytope.Core.Allele.Allele`
        :rtype: list(str)
        """
        return [self._represent(a) for a in alleles]

    @property
    def supportedAlleles(self):
        """
        A list of supported :class:`~epytope.Core.Allele.Allele`
        """
        return self.__alleles

    @property
    def name(self):
        """The name of the predictor"""
        return self.__name

    @property
    def command(self):
        """
        Defines the commandline call for external tool
        """
        return self.__command

    @property
    def supportedLength(self):
        """
        A list of supported :class:`~epytope.Core.Peptide.Peptide` lengths
        """
        return self.__supported_length

    def parse_external_result(self, file):
        """
        Parses external results and returns the result containing the predictors string representation
        of alleles and peptides.

        :param str file: The file path or the external prediction results
        :return: A dictionary containing the prediction results
        :rtype: dict
        """
        scores = defaultdict(defaultdict)
        alleles = []
        with open(file, "r") as f:
            for l in f:
                if l.startswith("#") or l.startswith("-") or l.strip() == "":
                    continue
                row = l.strip().split()
                if not row[0].isdigit():
                    continue
            
                epitope = row[PeptideIndex.NETCTLPAN_1_1]
                # Allele input representation differs from output representation. Needs to be in input representation to parse the output properly
                allele = row[HLAIndex.NETCTLPAN_1_1].replace('*','')
                comb_score = float(row[ScoreIndex.NETCTLPAN_1_1])
                if allele not in alleles:
                    alleles.append(allele)

                scores[allele][epitope] = comb_score

        result = {allele: {"Score": list(scores.values())[j]} for j, allele in enumerate(alleles)}
        
        return result

    def get_external_version(self, path=None):
        """
        Returns the external version of the tool by executing
        >{command} --version

        might be dependent on the method and has to be overwritten
        therefore it is declared abstract to enforce the user to
        overwrite the method. The function in the base class can be called
        with super()

        :param str path: Optional specification of executable path if deviant from :attr:`self.__command`
        :return: The external version of the tool or None if tool does not support versioning
        :rtype: str
        """
        return None

    def prepare_input(self, input, file):
        """
        Prepares input for external tools and writes them to file in the specific format

        No return value!

        :param: list(str) input: The :class:`~epytope.Core.Peptide.Peptide` sequences to write into file
        :param File file: File-handler to input file for external tool
        """
        file.write("\n".join(">pepe_%i\n%s" % (i, p) for i, p in enumerate(input)))


class PeptideIndex(IntEnum):
    """
    Specifies the index of the peptide sequence from the parsed output format
    """
    NETMHC_3_0 = 2
    NETMHC_3_4 = 2
    NETMHC_4_0 = 1
    NETMHCPAN_2_4 = 1
    NETMHCPAN_2_8 = 1
    NETMHCPAN_3_0 = 1
    NETMHCPAN_4_0 = 1
    NETMHCPAN_4_1 = 1
    NETMHCSTABPAN_1_0 = 1
    NETMHCII_2_2 = 2
    NETMHCII_2_3 = 2
    NETMHCIIPAN_3_0 = 1
    NETMHCIIPAN_3_1 = 1
    NETMHCIIPAN_4_0 = 1
    NETMHCIIPAN_4_1 = 1
    PICKPOCKET_1_1 = 2
    NETCTLPAN_1_1 = 3

class ScoreIndex(IntEnum):
    """
    Specifies the score index from the parsed output format
    """
    NETMHC_3_0 = 2
    NETMHC_3_4 = 3
    NETMHCPAN_2_4 = 3
    NETMHCPAN_2_8 = 3
    NETMHCPAN_3_0 = 4
    NETMHCPAN_4_0 = 5
    NETMHCPAN_4_1 = 5
    NETMHCSTABPAN_1_0 = 6
    NETMHCII_2_2 = 4
    NETMHCII_2_3 = 5
    NETMHCIIPAN_3_0 = 3
    NETMHCIIPAN_3_1 = 3
    NETMHCIIPAN_4_0 = 4
    NETMHCIIPAN_4_1 = 5
    PICKPOCKET_1_1 = 4
    NETCTLPAN_1_1 = 7

class RankIndex(IntEnum):
    """
    Specifies the rank index from the parsed output format if there is a rank score provided by the predictor
    """
    NETMHCPAN_2_8 = 5
    NETMHCPAN_3_0 = 6
    NETMHCPAN_4_0 = 7
    NETMHCPAN_4_1 = 6
    NETMHCSTABPAN_1_0 = 5
    NETMHCII_2_3 = 7
    NETMHCIIPAN_3_0 = 5
    NETMHCIIPAN_3_1 = 5
    NETMHCIIPAN_4_0 = 5
    NETMHCIIPAN_4_1 = 6

class Offset(IntEnum):
    """
    Specifies the offset of columns for multiple predicted HLA-alleles in the given predictors in order to
    correctly access score and rank per HLA-allele
    """
    NETMHC_4_0 = 3
    NETMHCPAN_2_8 = 3
    NETMHCPAN_3_0 = 4
    NETMHCPAN_4_0 = 5
    NETMHCPAN_4_1 = 4
    NETMHCSTABPAN_1_0_W_SCORE = 8
    NETMHCSTABPAN_1_0_WO_SCORE = 3
    NETMHCIIPAN_3_0 = 3
    NETMHCIIPAN_3_1 = 3
    NETMHCIIPAN_4_0 = 0
    NETMHCIIPAN_4_1 = 3

class HLAIndex(IntEnum):
    """
    Specifies the HLA-allele index in the parsed output of the predictor
    """
    NETMHCII_2_2 = 0
    NETMHCII_2_3 = 0
    PICKPOCKET_1_1 = 1
    NETCTLPAN_1_1 = 2

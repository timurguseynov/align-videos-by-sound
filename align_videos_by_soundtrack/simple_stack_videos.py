#! /bin/env python
# -*- coding: utf-8 -*-
"""
This module is intended as an example of one application of
"align_videos_by_soundtrack.align". Suppose that a certain concert is
shot by multiple people from multiple angles. In most cases, shooting
start and shooting end time have individual differences. It is now time
for "align_videos_by_soundtrack.align" to come. Based on the information
obtained from "align_videos_by_soundtrack.align", this script combines
movies of multiple angles in a tile shape with "hstack" and "vstack".
"""
from __future__ import unicode_literals
from __future__ import absolute_import

import json
import sys
import os
import logging

import numpy as np

from align_videos_by_soundtrack.align import SyncDetector
from align_videos_by_soundtrack.communicate import check_call
from .ffmpeg_filter_graph import Filter, ConcatWithGapFilterGraphBuilder


_logger = logging.getLogger(__name__)


class _StackVideosFilterGraphBuilder(object):
    def __init__(self, shape=(2, 2), w=960, h=540, sample_rate=44100):
        self._shape = shape
        self._builders = []
        for i in range(shape[0] * shape[1]):
            self._builders.append(
                ConcatWithGapFilterGraphBuilder(i, w, h, sample_rate))

    def set_paddings(self, idx, pre, post, v_filter_extra, a_filter_extra):
        self._builders[idx].add_video_gap(pre)
        self._builders[idx].add_audio_gap(pre)
        self._builders[idx].add_video_content(idx, v_filter_extra)
        self._builders[idx].add_audio_content(idx, a_filter_extra)
        self._builders[idx].add_video_gap(post)
        self._builders[idx].add_audio_gap(post)

    def build_each_streams(self):
        result = []
        _r = []
        len_stacks = len(self._builders)
        for i in range(len_stacks):
            _r.append(self._builders[i].build())
            result.append(_r[i][0])

        # filters string array, video maps, audio maps
        return result, [_ri[1] for _ri in _r], [_ri[2] for _ri in _r]

    def build_stack_videos(self, ivmaps):
        result = []

        ovmaps = ['[v]']
        # stacks for video
        fvstack = Filter()
        if self._shape[0] > 1:
            for i in range(self._shape[1]):
                fhstack = Filter()
                fhstack.iv.extend(ivmaps[i * self._shape[0]:(i + 1) * self._shape[0]])
                inputs = len(ivmaps[
                        i * self._shape[0]:(i + 1) * self._shape[0]])
                fhstack.add_filter(
                    "hstack",
                    inputs="%d" % inputs,
                    shortest="1")
                olab = "[{}v]".format("%d" % (i + 1) if self._shape[1] > 1 else "")
                fhstack.ov.append(olab)
                result.append(fhstack.to_str())

                fvstack.iv.append(olab)
        else:
            fvstack.iv.extend(ivmaps)

        if self._shape[1] > 1:
            # vstack
            fvstack.add_filter(
                "vstack", inputs="%d" % self._shape[1], shortest="1")
            fvstack.ov.append("[v]")
            result.append(fvstack.to_str())

        return result, ovmaps

    def build_amerge_audio(self, iamaps):
        #
        result = []

        # stacks for audio (amerge)
        nch = 2
        weight = 1 - np.array(
            [i // self._shape[0] for i in range(self._shape[0] * nch)])
        ch = [
            " + ".join([
                    "c%d" % (i)
                    for i in range(len(iamaps) * nch)
                    if weight[i % (self._shape[0] * nch)]]),
            " + ".join([
                    "c%d" % (i)
                    for i in range(len(iamaps) * nch)
                    if (1 - weight)[i % (self._shape[0] * nch)]])
            ]
        result.append("""\
{}
amerge=inputs={},
pan=stereo|\\
    c0 < {} |\\
    c1 < {}
[a]""".format("".join(iamaps),
              len(self._builders), ch[0], ch[1],))

        #
        return result, ['[a]']


def _build(args):
    shape = json.loads(args.shape) if args.shape else (2, 2)
    files = list(map(os.path.abspath, args.files))
    if len(files) < shape[0] * shape[1]:
        files = files + [files[i % len(files)]
                         for i in range(shape[0] * shape[1] - len(files))]
    else:
        files = files[:shape[0] * shape[1]]
    #
    a_filter_extra = json.loads(args.a_filter_extra) if args.a_filter_extra else {}
    v_filter_extra = json.loads(args.v_filter_extra) if args.v_filter_extra else {}
    #
    b = _StackVideosFilterGraphBuilder(
        shape=shape, w=args.w, h=args.h, sample_rate=args.sample_rate)
    with SyncDetector() as det:
        for i, inf in enumerate(det.align(files, max_misalignment=args.max_misalignment)):
            pre, post = inf[1]["pad"], inf[1]["pad_post"]
            if not (pre > 0 and post > 0):
                # FIXME:
                #  In this case, if we don't add paddings, we'll encount the following:
                #    [AVFilterGraph @ 0000000003146500] The following filters could not
                #choose their formats: Parsed_amerge_48
                #    Consider inserting the (a)format filter near their input or output.
                #    Error reinitializing filters!
                #    Failed to inject frame into filter network: I/O error
                #    Error while processing the decoded data for stream #3:0
                #    Conversion failed!
                #
                #  Just by adding padding just like any other stream, it seems we can
                #  avoid it, so let's add meaningless padding as a workaround.
                post = post + 1.0

            vf = ",".join(filter(None, [v_filter_extra.get(""), v_filter_extra.get("%d" % i)]))
            af = ",".join(filter(None, [a_filter_extra.get(""), a_filter_extra.get("%d" % i)]))
            b.set_paddings(i, pre, post, vf, af)

    #
    filters = []
    r0, vm0, am0 = b.build_each_streams()
    filters.extend(r0)
    if args.video_mode == "stack":
        r1, vm1 = b.build_stack_videos(vm0)
        filters.extend(r1)
    else:
        vm1 = vm0
    if args.audio_mode == "amerge":
        r2, am = b.build_amerge_audio(am0)
        filters.extend(r2)
    else:
        am = am0
    #
    filter_complex = ";\n\n".join(filters)
    #
    return files, filter_complex, vm1 + am


def main(args=sys.argv):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "files", nargs="+",
        help="The media files which contains both video and audio.")
    parser.add_argument(
        "-o", "--outfile", dest="outfile", default="merged.mp4",
        help="Specifying the output file. (default: %(default)s)")
    parser.add_argument(
        '--mode', choices=['script_bash', 'direct'], default='script_bash',
        help="""\
Switching whether to produce bash shellscript or to call ffmpeg directly. (default: %(default)s)""")
    #####
    parser.add_argument(
        '--audio_mode', choices=['amerge', 'multi_streams'], default='amerge',
        help="""\
Switching whether to merge audios or to keep each as multi streams. (default: %(default)s)""")
    #
    parser.add_argument(
        '--a_filter_extra', type=str,
        help="""\
Filter to add to the audio input stream. Pass in JSON format, in dictionary format \
(stream by key, filter by value). For example, '{"1": "volume = 0.5", "2": "loudnorm"}' etc. \
If the key is blank, it means all input streams. Only single input / single output \
filters can be used.""")
    ###
    parser.add_argument(
        '--video_mode', choices=['stack', 'multi_streams'], default='stack',
        help="""\
Switching whether to stack videos or to keep each as multi streams. (default: %(default)s)""")
    #
    parser.add_argument(
        '--v_filter_extra', type=str,
        help="""\
Filter to add to the video input stream. Pass in JSON format, in dictionary format \
(stream by key, filter by value). For example, '{"1": "boxblur=luma_radius=2:luma_power=1"}' etc. \
If the key is blank, it means all input streams. Only single input / single output \
filters can be used.""")
    #####
    parser.add_argument(
        '--max_misalignment', type=float, default=10*60,
        help="""\
See the help of alignment_info_by_sound_track. (default: %(default)d)""")
    parser.add_argument(
        '--shape', type=str, default="[2, 2]",
        help="The shape of the tile, like '[2, 2]'. (default: %(default)s)")
    parser.add_argument(
        '--sample_rate', type=int, default=44100,
        help="Sampling rate of the output file. (default: %(default)d)")
    parser.add_argument(
        '--width-per-cell', dest="w", type=int, default=960,
        help="Width of the cell. (default: %(default)d)")
    parser.add_argument(
        '--height-per-cell', dest="h", type=int, default=540,
        help="Height of the cell. (default: %(default)d)")
    extra_ffargs = [
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"
        ]
    args = parser.parse_args(args[1:])
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    files, fc, maps = _build(args)
    if args.mode == "script_bash":
        print("""\
#! /bin/sh

ffmpeg -y \\
  {} \\
  -filter_complex "
{}
" {} \\
  {} \\
  "{}"
""".format(" ".join(['-i "{}"'.format(f) for f in files]),
           fc,
           " ".join(["-map '%s'" % m for m in maps]),
           " ".join(extra_ffargs),
           args.outfile))
    else:
        cmd = ["ffmpeg", "-y"]
        for fn in files:
            cmd.extend(["-i", fn])
        cmd.extend(["-filter_complex", fc])
        for m in maps:
            cmd.extend(["-map", m])
        cmd.extend(extra_ffargs)
        cmd.append(args.outfile)

        check_call(cmd)


#
if __name__ == '__main__':
    main()

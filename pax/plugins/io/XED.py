"""
This plug-in reads raw waveform data from a Xenon100 XED file.
The XED file format is documented in Guillaume Plante's PhD thesis.
This is code does not use the libxdio C-library though.

At the moment this plugin supports:
    - sequential reading as well as searching for a particular event;
    - reading a single XED file or an entire dataset (in a directory);
    - one 'chunk' per event, it raises an exception if it sees more than one chunk;
    - zle0 sample encoding, not raw;
    - bzip2 or uncompressed chunk data compression, not any other compression scheme.

None of these would be very difficult to fix, if we ever intend to do any large-scale
reprocessing of the XED-files we have.

Some metadata from the XED file is stored in event['metadata'], see the end of this
code for details.
"""

import bz2
import io
import math

import numpy as np

from pax import units
from pax.datastructure import Event, Pulse
from pax.FolderIO import InputFromFolder

xed_file_header = np.dtype([
    ("dataset_name", "S64"),
    ("creation_time", "<u4"),
    ("first_event_number", "<u4"),
    ("events_in_file", "<u4"),
    ("event_index_size", "<u4")
])

xed_event_header = np.dtype([
    ("dataset_name", "S64"),
    ("utc_time", "<u4"),
    ("utc_time_usec", "<u4"),
    ("event_number", "<u4"),
    ("chunks", "<u4"),
    # This is where the 'chunk layer' starts... but there always seems to be one chunk per event
    # I'll always assume this is true and raise an exception otherwise
    ("type", "S4"),
    ("size", "<u4"),
    ("sample_precision", "<i2"),
    ("flags", "<u2"),               # indicating compression type.. I'll assume bzip2 always
    ("samples_in_event", "<u4"),
    ("voltage_range", "<f4"),
    ("sampling_frequency", "<f4"),
    ("channels", "<u4"),
])


class XedInput(InputFromFolder):

    file_extension = 'xed'

    def get_first_and_last_event_number(self, filename):
        """Return the first and last event number in file specified by filename"""
        with open(filename, 'rb') as xedfile:
            fmd = np.fromfile(xedfile, dtype=xed_file_header, count=1)[0]
            return (fmd['first_event_number'],
                    fmd['first_event_number'] + fmd['events_in_file'] - 1)

    def close(self):
        """Close the currently open file"""
        self.current_xedfile.close()

    def open(self, filename):
        """Opens an XED file so we can start reading events"""
        self.current_xedfile = open(filename, 'rb')

        # Read in the file metadata
        self.file_metadata = np.fromfile(self.current_xedfile,
                                         dtype=xed_file_header,
                                         count=1)[0]
        self.event_positions = np.fromfile(self.current_xedfile,
                                           dtype=np.dtype("<u4"),
                                           count=self.file_metadata['event_index_size'])

        # Handle for special case of last XED file
        # Index size is larger than the actual number of events written:
        # The writer didn't know how many events there were left at s
        if self.file_metadata['events_in_file'] < self.file_metadata['event_index_size']:
            self.log.info(
                ("The XED file claims there are %d events in the file, "
                 "while the event position index has %d entries. \n"
                 "Is this the last XED file of a dataset?") %
                (self.file_metadata['events_in_file'], self.file_metadata['event_index_size'])
            )
            self.event_positions = self.event_positions[:self.file_metadata['events_in_file']]

    def get_single_event_in_current_file(self, event_position):

        # Seek to the requested event
        self.current_xedfile.seek(self.event_positions[event_position])

        # Read event metadata, check if we can read this event type.
        event_layer_metadata = np.fromfile(self.current_xedfile,
                                           dtype=xed_event_header,
                                           count=1)[0]

        if event_layer_metadata['chunks'] != 1:
            raise NotImplementedError("Can't read this XED file: event with %s chunks found!"
                                      % event_layer_metadata['chunks'])

        # Check if voltage range and digitizer dt are the same as in the settings
        # If not, raise error. Would be simple matter to change settings dynamically, but that's weird.
        values_to_check = (
            ('Voltage range',   self.config['digitizer_voltage_range'],
             event_layer_metadata['voltage_range']),
            ('Digitizer dt',    self.config['sample_duration'],
             1 / (event_layer_metadata['sampling_frequency'] * units.Hz)),
        )
        for name, ini_value, xed_value in values_to_check:
            if ini_value != xed_value:
                raise RuntimeError(
                    '%s from XED event metadata (%s) is different from ini file setting (%s)!'
                    % (name, xed_value, ini_value)
                )

        # Start building the event
        event = Event(
            n_channels=self.config['n_channels'],
            start_time=int(
                event_layer_metadata['utc_time'] * units.s +
                event_layer_metadata['utc_time_usec'] * units.us
            ),
            sample_duration=int(self.config['sample_duration']),
            length=event_layer_metadata['samples_in_event']
        )
        event.dataset_name = self.file_metadata['dataset_name'].decode("utf-8")
        event.event_number = int(event_layer_metadata['event_number'])

        if event_layer_metadata['type'] == b'raw0':
            # Grok 'raw' XEDs - these probably come from the LED calibration

            # 4 unused bytes at start (part of 'chunk header')
            self.current_xedfile.read(4)

            # Data is just a big bunch of samples from one channel, then next channel, etc
            # Each channel has an equal number of samples.
            data = np.fromfile(self.current_xedfile,
                               dtype='<i2',
                               count=event_layer_metadata['channels'] *
                                     event_layer_metadata['samples_in_event'])
            data = np.reshape(data, (event_layer_metadata['channels'],
                                     event_layer_metadata['samples_in_event']))
            for ch_i, chdata in enumerate(data):
                event.pulses.append(Pulse(
                    channel=ch_i + 1,       # +1 as first channel is 1 in Xenon100
                    left=0,
                    raw_data=chdata
                ))
        elif event_layer_metadata['type'] == b'zle0':
            # Read the channel bitmask to find out which channels are included in this event.
            # Lots of possibilities for errors here: 4-byte groupings, 1-byte groupings, little-endian...
            # Checked (for 14 events); agrees with channels from
            # LibXDIO->Moxie->MongoDB->MongoDBInput plugin
            mask_bytes = 4 * math.ceil(event_layer_metadata['channels'] / 32)
            mask_bits = np.unpackbits(np.fromfile(self.current_xedfile,
                                                  dtype='uint8',
                                                  count=mask_bytes))
            # +1 as first pmt is 1 in Xenon100
            channels_included = [i + 1 for i, bit in enumerate(reversed(mask_bits))
                                 if bit == 1]

            # Decompress the event data (actually, the data from a single 'chunk')
            # into fake binary file (io.BytesIO)
            # 28 is the chunk header size.
            data_to_decompress = self.current_xedfile.read(event_layer_metadata['size'] - 28 - mask_bytes)
            try:
                chunk_fake_file = io.BytesIO(bz2.decompress(data_to_decompress))
            except OSError:
                # Maybe it wasn't compressed after all? We can at least try
                # TODO: figure this out from flags
                chunk_fake_file = io.BytesIO(data_to_decompress)

            # Loop over all channels in the event to get the pulses
            for channel_id in channels_included:
                # Read channel size (in 4bit words), subtract header size, convert
                # from 4-byte words to bytes
                channel_data_size = int(4 * (np.fromstring(chunk_fake_file.read(4),
                                                           dtype='<u4')[0] - 1))

                # Read the channel data into another fake binary file
                channel_fake_file = io.BytesIO(chunk_fake_file.read(channel_data_size))

                # Read the channel data control word by control word.
                # sample_position keeps track of where in the waveform a new
                # pulse should be placed.
                sample_position = 0
                while 1:
                    # Is there a new control word?
                    control_word_string = channel_fake_file.read(4)
                    if not control_word_string:
                        break

                    # Control words starting with zero indicate a number of sample PAIRS to skip
                    control_word = int(np.fromstring(control_word_string,
                                                     dtype='<u4')[0])
                    if control_word < 2 ** 31:
                        sample_position += 2 * control_word
                        continue

                    # Control words starting with one indicate a number of sample PAIRS follow
                    else:
                        # Subtract the control word flag
                        data_samples = 2 * (control_word - (2 ** 31))

                        # Note endianness
                        samples_pulse = np.fromstring(channel_fake_file.read(2 * data_samples),
                                                      dtype="<i2")

                        event.pulses.append(Pulse(
                            channel=channel_id,
                            left=sample_position,
                            raw_data=samples_pulse
                        ))
                        sample_position += len(samples_pulse)

        else:
            raise NotImplementedError("XED type %s not supported" % event_layer_metadata['type'])

        # Check we have read all data for this event

        if event_position != len(self.event_positions) - 1:
            current_pos = self.current_xedfile.tell()
            should_be_at_pos = self.event_positions[event_position + 1]
            if current_pos != should_be_at_pos:
                raise RuntimeError("Error during XED reading: after reading event %d from file "
                                   "(event number %d) we should be at position %d, but we are at position %d!" % (
                                       event_position, event.event_number, should_be_at_pos, current_pos))

        return event

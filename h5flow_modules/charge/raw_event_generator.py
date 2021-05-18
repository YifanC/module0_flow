import numpy as np
import h5py

from h5flow.core import H5FlowGenerator, H5FlowStage

from event_builder import *

class RawEventGenerator(H5FlowGenerator):
    default_buffer_size = 38400
    default_nhit_cut = 100
    default_sync_noise_cut = 100000
    default_sync_noise_cut_enabled = True
    default_event_builder_class = 'SymmetricWindowEventBuilder'
    default_event_builder_config = dict()

    raw_event_dtype = np.dtype([
        ('evid', 'u4'),
        ('unix_ts', 'u8')
        ])

    def __init__(self, **params):
        super(RawEventGenerator,self).__init__(**params)

        # set up parameters
        self.buffer_size = params.get('buffer_size', self.default_buffer_size)
        self.nhit_cut = params.get('nhit_cut', self.default_nhit_cut)
        self.sync_noise_cut = params.get('sync_noise_cut', self.default_sync_noise_cut)
        self.sync_noise_cut_enabled = params.get('sync_noise_cut_enabled', self.default_sync_noise_cut_enabled)
        self.event_builder_class = params.get('event_builder_class', self.default_event_builder_class)
        self.event_builder_config = params.get('event_builder_config', self.default_event_builder_config)

        # create event builder
        self.event_builder = globals()[self.event_builder_class](**self.event_builder_config)

        # set up input file
        self.input_fh = h5py.File(self.input_filename, 'r', driver='mpio', comm=self.comm) # open in parallel
        self.packets = self.input_fh['packets']

        # set up new data objects
        self.name = self.dset_name.split('/')[0]
        self.packets_dtype = self.packets.dtype
        self.packets_dset_name = f'{self.name}/packets'
        self.raw_event_dset_name = self.dset_name

        # set up loop variables
        if self.start_position is None:
            self.start_position = 0
        if self.end_position is None or self.end_position > len(self.packets):
            self.end_position = len(self.packets)
        self.slices = [slice(st, st + self.buffer_size) for st in range(self.start_position, self.end_position, self.size * self.buffer_size)]
        self.iteration = 0

        if self.rank == 0:
            self.last_unix_ts = self.packets[np.argmax(self.packets.fields('packet_type')==4)] # first timestamp packet
        else:
            self.last_unix_ts = None
        self.last_unix_ts = self.comm.bcast(self.last_unix_ts, root=0) # distribute result from root thread to all

    def __len__(self):
        return len(self.slices)

    def init(self):
        super(RawEventGenerator,self).init()

        # initialize data objects
        self.data_manager.create_dset(self.raw_event_dset_name, dtype=self.raw_event_dtype)
        self.data_manager.create_dset(self.packets_dset_name, dtype=self.packets_dtype)
        self.data_manager.create_ref(self.raw_event_dset_name, self.packets_dset_name)
        self.data_manager.set_attrs(self.raw_event_dset_name,
            classname=self.classname,
            class_version=self.class_version,
            nhit_cut=self.nhit_cut,
            sync_noise_cut=self.sync_noise_cut,
            sync_noise_cut_enabled=self.sync_noise_cut_enabled,
            event_builder_class=self.event_builder_class,
            event_builder_class_version=self.event_builder.version,
            start_position=self.start_position,
            end_position=self.end_position,
            input_filename=self.input_filename,
            **self.event_builder.get_config()
            )

    def next(self):
        if self.iteration >= len(self.slices):
            return H5FlowGenerator.EMPTY
        sl = self.slices[self.iteration]
        self.iteration += 1

        block = self.packets[sl]

        mask = (block['valid_parity'].astype(bool) & (block['packet_type'] == 0)) # data packets
        mask = mask | (block['packet_type'] == 4) # timestamp packets
        mask = mask | (block['packet_type'] == 7) # external trigger packets
        mask = mask | (block['packet_type'] == 6) # sync packets

        packet_buffer = np.copy(block[mask])
        packet_buffer = np.insert(packet_buffer, [0], self.last_unix_ts)

        # find unix timestamp groups
        ts_mask = packet_buffer['packet_type'] == 4
        ts_grps = np.split(packet_buffer, np.argwhere(ts_mask).flatten())
        unix_ts = np.concatenate([[ts_grp[0]]*len(ts_grp[1:]) for ts_grp in ts_grps if len(ts_grp) > 1], axis=0)
        packet_buffer = packet_buffer[~ts_mask]
        packet_buffer['timestamp'] = packet_buffer['timestamp'].astype(int) % (2**31) # ignore 32nd bit from pacman triggers
        self.last_unix_ts = unix_ts[-1]

        # run event builder
        events, event_unix_ts = self.event_builder.build_events(packet_buffer, unix_ts)

        # apply nhit cut
        filtered = list(filter(lambda x: len(x[0]) > self.nhit_cut, zip(events, event_unix_ts)))
        if len(filtered):
            events, event_unix_ts = zip(*filtered)
        else:
            events, event_unix_ts = list(),list()

        # apply sync filter
        if self.sync_noise_cut_enabled:
            filtered = list(filter(
                lambda x: np.min(x[0]['timestamp']) > self.sync_noise_cut,
                zip(events, event_unix_ts)
                ))
            if len(filtered):
                events, event_unix_ts = zip(*filtered)
            else:
                events, event_unix_ts = list(),list()

        # write event to file
        raw_event_array = np.empty((len(events),), dtype=self.raw_event_dtype)
        raw_event_slice = self.data_manager.reserve_data(self.raw_event_dset_name, len(events))
        raw_event_idcs = np.arange(raw_event_slice.start, raw_event_slice.stop)
        raw_event_array['unix_ts'] = [p[0]['timestamp'] for p in event_unix_ts]
        raw_event_array['evid'] = raw_event_idcs
        self.data_manager.write_data(self.raw_event_dset_name, raw_event_slice, raw_event_array)

        # write packets to file
        packets_array = np.concatenate(events, axis=0) if len(events) else np.empty((0,))
        packets_slice = self.data_manager.reserve_data(self.packets_dset_name, len(packets_array))
        packets_idcs = np.arange(packets_slice.start, packets_slice.stop)
        self.data_manager.write_data(self.packets_dset_name, packets_slice, packets_array)

        # set up references
        #   just event -> packet refs for now
        self.data_manager.reserve_ref(self.raw_event_dset_name, self.packets_dset_name, raw_event_slice)
        ref = [packets_idcs[i:i+len(ev)] for i,ev in enumerate(events)]
        self.data_manager.write_ref(self.raw_event_dset_name, self.packets_dset_name, raw_event_slice, ref)

        return raw_event_slice



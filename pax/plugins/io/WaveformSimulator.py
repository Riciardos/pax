"""
Plugins to interface with the integrated waveform simulator (FaX)
This is I/O stuff only: truth file writing, instruction reading, etc.
There is no physics here, all that is in pax.simulation.
"""

import os
import csv

import numpy as np

import pandas
from pax import plugin, units, utils


def uniform_circle_rv(radius, n_samples=None):
    """Sample x,y uniformly in acircle with radius"""
    
    if n_samples is None:
        just_give_one = True
        n_samples = 1
    else:
        just_give_one = False
    
    xs = []
    ys = []
    
    for sample_i in range(n_samples):
        while True:
            x = np.random.uniform(-radius, radius)
            y = np.random.uniform(-radius, radius)
            if x**2 + y**2 <= radius**2:
                break
        xs.append(x)
        ys.append(y)
    
    if just_give_one:
        return xs[0], ys[0]
    else:
        return xs, ys


class WaveformSimulator(plugin.InputPlugin):
    
    """ Common I/O for waveform simulator plugins
    """
    
    def startup(self):
        self.all_truth_peaks = []
        self.simulator = self.processor.simulator
        # The simulator's internal config was already intialized in the core
    
    def shutdown(self):
        self.log.debug("Write the truth peaks to %s" % self.config['truth_file_name'])
        output = pandas.DataFrame(self.all_truth_peaks)
        output.to_csv(self.config['truth_file_name'], index_label='fax_truth_peak_id')
    
    def store_true_peak(self, peak_type, t, x, y, z, photon_times, electron_times=()):
        """ Saves the truth information about a peak (s1 or s2)
        """
        true_peak = {
            'instruction':      self.current_instruction,
            'repetition':       self.current_repetition,
            'event':            self.current_event,
            'peak_type':        peak_type,
            'x': x, 'y': y, 'z': z,
            't_interaction':     t,
        }
        for name, times in (('photon', photon_times), ('electron', electron_times)):
            if len(times) != 0:
                # This signal type doesn't exist in this peak
                true_peak.update({
                    ('n_%ss' % name):         len(times),
                    ('t_mean_%ss' % name):    np.mean(times),
                    ('t_first_%s' % name):    np.min(times),
                    ('t_last_%s' % name):     np.max(times),
                    ('t_sigma_%ss' % name):   np.std(times),
                })
            else:
                true_peak.update({
                    ('n_%ss' % name):         float('nan'),
                    ('t_mean_%ss' % name):    float('nan'),
                    ('t_first_%s' % name):    float('nan'),
                    ('t_last_%s' % name):     float('nan'),
                    ('t_sigma_%ss' % name):   float('nan'),
                })
        self.truth_peaks.append(true_peak)

    def s2(self, electrons, t=0., x=0., y=0., z=0.):
        # s2() now returns both the photon times and the hitpattern
        electron_times = self.simulator.s2_electrons(electrons_generated=electrons, t=t, z=z)
        if not len(electron_times):
            return ([],None)
        photon_times = self.simulator.s2_scintillation(electron_times, x, y)
        if not len(photon_times):
            return ([],None)
        self.store_true_peak('s2', t, x, y, z, photon_times, electron_times)
        # Generate S2 hitpattern "at the anode": cue for  simulator to use the S2 LCE map
        return (photon_times,self.simulator.make_hitpattern(photon_times,x=x,y=y,z=-self.config['gate_to_anode_distance']))

    def s1(self, photons, recoil_type, t=0., x=0., y=0., z=0.):
        # s1() now returns both the photon times and the hitpattern		
        """
        :param photons: total # of photons generated in the S1
        :param recoil_type: 'ER' for electronic recoil, 'NR' for nuclear recoil
        :param t: Time at which the interaction occurs, i.e. offset for arrival times.
                Defaults to s1_default_recombination_time.
        :return: start_time, channel_waveforms
        """
        photon_times = self.simulator.s1_photons(photons, recoil_type, x, y, z, t)
        if not len(photon_times):
            return ([], None)
        self.store_true_peak('s1', t, x, y, z, photon_times)
        return (photon_times,self.simulator.make_hitpattern(photon_times,x=x,y=y,z=z))
    
    def after_pulse(self, photon_times, x=0., y=0., z=0.):
            # added by Qing Lin for the after pulses
            # """
            # :param photon_times: the times of the photons that have potential to generate the photo-ionized electrons
            # :param x, y, z: the primary s1 or s2 position (currently the after pulse X-Ys still are randomly generated).
            # :return: a list of hitpatterns
            # """

        # after pulse can generate subsequent after pulse as well. This claims how many loops we want to make in simulation        
        n_fold_generation = int(self.config['n_fold_generation_after_pulse']) 
        if not len(photon_times):
            return None

        # output
        after_pulse_hitpatterns = []
        # contain the primary photon timings, and will be updated every loop
        primary_photon_times = photon_times
        primary_photon_Xs = [x]*len(primary_photon_times)
        primary_photon_Ys = [y]*len(primary_photon_times)
        primary_photon_Zs = [z]*len(primary_photon_times)
        for fold_id in range(0, n_fold_generation):
            if not len(primary_photon_times):
                break

            # for updating primary photon_xxx
            current_loop_photon_times = []
            current_loop_photon_Xs = []
            current_loop_photon_Ys = []
            current_loop_photon_Zs = []
            # generate the electron times, Xs, Ys, Zs
            after_pulse_electron_times, after_pulse_electron_Xs, after_pulse_electron_Ys, after_pulse_electron_Zs = self.simulator.after_pulse_electrons(primary_photon_times, primary_photon_Xs, primary_photon_Ys, primary_photon_Zs)

            # loop over the generated electrons
            for after_pulse_electron_id in range(0, len(after_pulse_electron_times)):
                after_pulse_electron_time = after_pulse_electron_times[after_pulse_electron_id]
                after_pulse_X = after_pulse_electron_Xs[after_pulse_electron_id]
                after_pulse_Y = after_pulse_electron_Ys[after_pulse_electron_id]
                after_pulse_Z = after_pulse_electron_Zs[after_pulse_electron_id]
                
                after_pulse_photon_times = self.simulator.s2_scintillation([after_pulse_electron_time], after_pulse_X, after_pulse_Y)
                after_pulse_hitpatterns.append( self.simulator.make_hitpattern(after_pulse_photon_times, after_pulse_X, after_pulse_Y) )
                current_loop_photon_times.extend(after_pulse_photon_times)
                current_loop_photon_Xs.extend([after_pulse_X]*len(after_pulse_photon_times) )
                current_loop_photon_Ys.extend([after_pulse_Y]*len(after_pulse_photon_times) )
                current_loop_photon_Zs.extend([after_pulse_Z]*len(after_pulse_photon_times) )

            # replace the primary with
            primary_photon_times = current_loop_photon_times
            primary_photon_Xs = current_loop_photon_Xs
            primary_photon_Ys = current_loop_photon_Ys
            primary_photon_Zs = current_loop_photon_Zs
        
        return after_pulse_hitpatterns
    
    def get_instructions_for_next_event(self):
        raise NotImplementedError()
    
    def simulate_single_event(self, instructions):
        self.truth_peaks = []
        
        hitpatterns = []
        for q in instructions:
            self.log.debug("Simulating %s photons and %s electrons at %s cm depth, at t=%s ns" % (
                q['s1_photons'], q['s2_electrons'], q['depth'], q['t']
            ))

            # Should we choose x and yrandomly?
            if q['x'] == 'random':
                x, y = uniform_circle_rv(self.config['tpc_radius'])
            else:
                x = float(q['x'])
                y = float(q['y'])
            
            if q['depth'] == 'random':
                z = np.random.uniform(0, self.config['tpc_length'])
            else:
                z = float(q['depth']) * units.cm

            # Modified by Qing Lin
            # Implement the after pulse
            if int(q['s1_photons']):
                # get the main s1 photon times
                s1_photon_times, s1_hitpattern = self.s1(photons=int(q['s1_photons']),recoil_type=q['recoil_type'],t=float(q['t']),x=x,y=y,z=z)
                #based on the s1_photon_times create the after pulses
                #s1_after_pulse_hitpatterns contain all the hitpattern for each photonionzied electron
                s1_after_pulse_hitpatterns = self.after_pulse(s1_photon_times, x=x, y=y,z=z)
                #append s1
                hitpatterns.append(s1_hitpattern)
                #append after pulses
                for s1_after_pulse_hitpattern in s1_after_pulse_hitpatterns:
                    hitpatterns.append(s1_after_pulse_hitpattern)
            
            if int(q['s2_electrons']):
                # get the main s2 photon times
                s2_photon_times, s2_hitpattern = self.s2(electrons=int(q['s2_electrons']),t=float(q['t']),x=x,y=y,z=z)
                #based on the s2_photon_times create the after pulses
                #s2_after_pulse_hitpatterns contain all the hitpattern for each photonionzied electron
                s2_after_pulse_hitpatterns = self.after_pulse(s2_photon_times, x=x, y=y,z=z)
                #append s2
                hitpatterns.append(s2_hitpattern)
                #append after pulses
                for s2_after_pulse_hitpattern in s2_after_pulse_hitpatterns:
                    hitpatterns.append(s2_after_pulse_hitpattern)
        
        hitpatterns = [h for h in hitpatterns if h is not None]
        if len(hitpatterns):
            # Combine the hitpatterns by their overloaded addition operator (sorry)
            big_hitpattern = sum(hitpatterns)
        else:
            # Create an empty hitpattern
            big_hitpattern = None
        event = self.simulator.to_pax_event(big_hitpattern)
        if hasattr(self, 'dataset_name'):
            event.dataset_name = self.dataset_name
        event.event_number = self.current_event

        # Add start time offset to all times in the truth information peak
        # Can't be done at the time of peak creation, it is only known now...
        # TODO: That's no longer true! so fix it
        for p in self.truth_peaks:
            for key in p.keys():
                if key[:2] == 't_' and key[2:7] != 'sigma':
                    if p[key] == '':
                        continue
                    p[key] += self.config['event_padding']
        self.all_truth_peaks.extend(self.truth_peaks)
        
        return event
    
    def get_events(self):
        
        for instruction_number, instructions in enumerate(self.get_instructions_for_next_event()):
            self.current_instruction = instruction_number
            for repetition_i in range(self.config['event_repetitions']):
                self.current_repetition = repetition_i
                self.current_event = instruction_number * self.config['event_repetitions'] + repetition_i
                self.log.debug('Instruction %s, iteration %s, event number %s' % (
                    instruction_number, repetition_i, self.current_event))
                event = self.simulate_single_event(instructions)
                if event is not None:
                    yield event


class WaveformSimulatorFromCSV(WaveformSimulator):
    
    def startup(self):
        """
        The startup routine of the WaveformSimulatorFromCSV plugin
        """

        # Open the instructions file
        filename = self.config['input_name']
        self.dataset_name = os.path.basename(filename)
        self.instructions_file = open(utils.data_file_name(filename), 'r')
        #
        # Slurp the entire instructions file, so we know the number of events
        self.instruction_reader = csv.DictReader(self.instructions_file)
        self.instructions = []
        #
        # Loop over lines, make instructions
        instruction_number = 0
        instruction = []
        for p in self.instruction_reader:
            if int(p['instruction']) == instruction_number:
                # Deposition is part of the previous instruction
                instruction.append(p)
            else:
                # New deposition reached!
                if instruction:
                    self.instructions.append(instruction)
                instruction_number = int(p['instruction'])
                instruction = [p]
        # For the final instruction
        self.instructions.append(instruction)
        
        self.number_of_events = len(self.instructions) * self.config['event_repetitions']
        WaveformSimulator.startup(self)
    
    def shutdown(self):
        self.instructions_file.close()
        WaveformSimulator.shutdown(self)
    
    def get_instructions_for_next_event(self):
        for instr in self.instructions:
            yield instr


class WaveformSimulatorFromNEST(WaveformSimulator):
    
    variables = (
        # Fax name        #Root name    #Conversion factor (multiplicative)
        ('x',             'Nest_x',     0.1),
        ('y',             'Nest_y',     0.1),
        ('depth',         'Nest_z',     -0.1),
        ('s1_photons',    'Nest_nph',   1),
        ('s2_electrons',  'Nest_nel',   1),
        ('t',             'Nest_t',     10 ** 9),
        ('recoil_type',   'Nest_nr',    1),
    )
    
    def startup(self):
        self.log.warning('This plugin is completely untested and will probably crash!')
        filename = self.config['input_name']
        import ROOT
        self.f = ROOT.TFile(utils.data_file_name(filename))
        self.t = self.f.Get("t1")  # For Xerawdp use T1, for MC t1
        WaveformSimulator.startup(self)
        self.number_of_events = self.t.GetEntries() * self.config['event_repetitions']
    
    def get_instructions_for_next_event(self):
        for event_i in range(self.number_of_events):
            self.t.GetEntry(event_i)

            # Get stuff from root files
            values = {}
            for (variable_name, root_thing_name, _) in self.variables:
                values[variable_name] = getattr(self.t, root_thing_name)

            # Convert to peaks dictionary
            npeaks = len(values[self.variables[0][0]])
            peaks = []
            for i in range(npeaks):
                peaks.append({'instruction': event_i})
                for (variable_name, _, conversion_factor) in self.variables:
                    peaks[-1][variable_name] = values[variable_name][i] * conversion_factor
            
            for p in peaks:
                # Subtract depth of gate mesh, see xenon:xenon100:mc:roottree, bottom of page
                p['depth'] -= 2.15 + 0.25
                # Fix ER / NR label
                if p['recoil_type'] != 0:
                    p['recoil_type'] = 'NR'
                else:
                    p['recoil_type'] = 'ER'
            # Sort by time
            peaks.sort(key=lambda p: p['t'])
            
            yield peaks

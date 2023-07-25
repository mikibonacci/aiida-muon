# -*- coding: utf-8 -*-
import numpy as np
from aiida import orm
from aiida.engine import WorkChain, calcfunction, if_
from aiida.plugins import CalculationFactory, DataFactory, WorkflowFactory
from aiida_quantumespresso.common.types import RelaxType
from aiida_quantumespresso.workflows.protocols.utils import recursive_merge
from aiida_quantumespresso.workflows.protocols.utils import ProtocolMixin
from aiida_quantumespresso.common.types import ElectronicType, RelaxType, SpinType
from pymatgen.core import Structure
from pymatgen.electronic_structure.core import Magmom
from aiida.common import AttributeDict
from aiida_quantumespresso.utils.mapping import prepare_process_inputs


#MB
from aiida_musconv.workflows.musconv import MusconvWorkChain
from aiida_musconv.workflows.musconv import input_validator as musconv_input_validator

from .niche import Niche
from .utils import (
    check_get_hubbard_u_parms,
    cluster_unique_sites,
    compute_dip_field,
    get_collinear_mag_kindname,
    get_struct_wt_distortions,
    load_workchain_data,
)

PwBaseWorkChain = WorkflowFactory('quantumespresso.pw.base')
PwRelaxWorkChain = WorkflowFactory('quantumespresso.pw.relax')

class FindMuonWorkChain(ProtocolMixin, WorkChain):
    """
    FindMuonWorkChain finds the candidate implantation site for a positive muon.
    It first performs DFT relaxation calculations for a set of initial muon sites.
    It then analyzes the results of these calculations and finds candidate muon sites.
    If there are magnetic inequivalent sites not initially, they are recalculated
    It further calculates the muon contact hyperfine field at these candidate sites.
    """

    @classmethod
    def define(cls, spec):
        """Specify inputs and outputs."""
        super().define(spec)

        spec.input(
            "structure",
            valid_type=orm.StructureData,
            required=True,
            help="Input initial structure",
        )

        spec.input(
            "sc_matrix",
            valid_type=orm.List,
            required=False,   #MB put False by MB
            help=" List of length 1 for supercell size ",
        )

        spec.input(
            "mu_spacing",
            valid_type=orm.Float,
            default=lambda: orm.Float(1.0),
            required=False,
            help="Minimum distance in Angstrom between two starting muon positions  generated on a grid.",
        )

        # read as list or array?
        spec.input(
            "magmom",
            valid_type=orm.List,
            required=False,
            help="List of 3D magnetic moments in Bohr magneton of the corresponding input structure if magnetic",
        )
        
        spec.input(
            "mag_dict",
            valid_type=orm.Dict,
            required=False,
            #non_db=True,
            help="magnetic dict created in protocols.",
        )

        spec.input(
            "pp_code",
            valid_type=orm.Code,
            required=False,
            help="The pp.x code-computer for post processing only if magmom is supplied",
        )

        spec.input(
            "pseudo_family",
            valid_type=orm.Str,
            default=lambda: orm.Str("SSSP/1.2/PBE/efficiency"),
            required=False,
            help="The label of the pseudo family",
        )

        spec.input(
            "kpoints_distance",
            valid_type=orm.Float,
            default=lambda: orm.Float(0.301),
            help="The minimum desired distance in 1/Å between k-points in reciprocal space.",
        )

        spec.input(
            "qe.hubbard_u",
            valid_type=orm.Bool,
            default=lambda: orm.Bool(True),
            required=False,
            help="To check and get Hubbard U value or not",
        )

        spec.input(
            "charge_supercell",
            valid_type=orm.Bool,
            default=lambda: orm.Bool(True),
            required=False,
            help="To run charged supercell for positive muon or not (neutral supercell)",
        )
        
        #TODO: decide if you want to retain this inputs... actually I think are not needed and should be set in the qe 

        '''spec.input(
            "parameters",
            valid_type=orm.Dict,
            required=False,
            help=" Preferred pw.x set of parameters, otherwise it is set automatically",
        )'''

        spec.expose_inputs(
            PwRelaxWorkChain,
            namespace="relax",
            exclude=("structure"),
            namespace_options={
                'required': False, 
                'populate_defaults': False,
                'help': 'Inputs for SCF calculations.',
            },
        )  # use the  pw relax workflow
        
        #to run final scf
        spec.expose_inputs(
            PwBaseWorkChain,
            namespace="pwscf",
            namespace_options={
                'required': False, 
                'populate_defaults': False,
                'help': 'Inputs for final SCF calculation with the muon at the origin.',
            },
            exclude=("pw.structure", "kpoints"),
        )  # 
        
        spec.input(
            "qe_settings",
            valid_type=orm.Dict,
            required=False,
            help=" Preferred settings for the calc, otherwise default is used",
        )
        
        
        '''spec.input(
            "qe_metadata",
            valid_type=orm.Dict,
            required=False,
            help=" Preferred metadata and scheduler options for relax calc, otherwise  default in the Code is used",
        )'''

        spec.input(
            "pp_metadata",
            valid_type= dict, 
            non_db=True,
            required=False,
            help=" Preferred metadata and scheduler options for pp.x",
        )

        spec.input(
            "musconv_metadata",
            valid_type=dict,
            non_db=True,
            required=False,
            help=" Preferred metadata and scheduler options for musconv",
        )

        #MB trying to add in the workflow the MusconvWorkchain. 
        #MB activate it only if sc_matrix not present.
        spec.expose_inputs(
            MusconvWorkChain,
            namespace="musconv",
            exclude=("structure", "pseudos",),
            namespace_options={
                'required': False, 'populate_defaults': False,
                'help': 'the preprocess MusconvWorkChain step, if needed.',
            },
        )  # use the  pw calcjob
        
        spec.inputs.validator = recursive_consistency_check
        
        spec.outline(
            if_(cls.not_converged_supercell)(     
                cls.converge_supercell,         
                cls.check_supercell_convergence,          
            ),
            cls.setup,
            cls.get_initial_muon_sites,
            if_(cls.not_from_protocols)(
                cls.setup_magnetic_hubbardu_dict,
            ),
            cls.get_initial_supercell_structures,
            #cls.setup_pw_overrides,   TODO step done as input validator
            cls.compute_supercell_structures,
            cls.collect_relaxed_structures,
            cls.analyze_relaxed_structures,
            if_(cls.new_struct_after_analyze)(
                cls.compute_supercell_structures,
                cls.collect_relaxed_structures,
                cls.collect_all_results,
            ),
            if_(cls.structure_is_magnetic)(
                cls.run_final_scf_mu_origin,  # to be removed if better alternative
                cls.compute_spin_density,
                cls.inspect_get_contact_hyperfine,
                cls.get_dipolar_field,
                cls.set_hyperfine_outputs,
            ),
            cls.set_relaxed_muon_outputs,
        )

        spec.exit_code(
            404,
            "ERROR_MUSCONV_CALC_FAILED",
            message="The MusconvWorkChain subprocesses failed",
        )

        spec.exit_code(
            405,
            "ERROR_RELAX_CALC_FAILED",
            message="One of the PwRelaxWorkChain subprocesses failed",
        )

        spec.exit_code(
            406,
            "ERROR_BASE_CALC_FAILED",
            message="One of the PwBaseWorkChain subprocesses failed",
        )

        spec.exit_code(
            407,
            "ERROR_PP_CALC_FAILED",
            message="One of the PPWorkChain subprocesses failed",
        )

        spec.exit_code(
            407,
            "ERROR_NO_SUPERCELLS",
            message="No supercells available: try to decrease mu_spacing.",
        )

        # TODO: more exit codes catch errors and throw exit codes
        #MB add exit code for the musconv.

        spec.output("all_index_uuid", valid_type=orm.Dict, required=True)

        spec.output("all_sites", valid_type=orm.Dict, required=True)

        spec.output("unique_sites", valid_type=orm.Dict, required=True)

        spec.output(
            "unique_sites_hyperfine", valid_type=orm.Dict, required=False
        )  # return only when magnetic
        
        spec.output(
            "unique_sites_dipolar", valid_type=orm.List, required=False
        )  # return only when magnetic
        
        
    @classmethod
    def get_builder_from_protocol(
        cls,
        pw_code,
        structure,
        protocol=None,
        overrides={},
        relax_musconv=False,
        magmom=None,
        options=None,
        sc_matrix=None,
        mu_spacing=1.0,
        kpoints_distance=0.401,
        charge_supercell=True,
        pseudo_family="SSSP/1.2/PBE/efficiency",
        pp_code = None,
        **kwargs,
    ):
        """Return a builder prepopulated with inputs selected according to the chosen protocol.

        :param pw_code: the ``Code`` instance configured for the ``quantumespresso.pw`` plugin.
        :param structure: the ``StructureData`` instance to use.
        :param protocol: protocol to use, if not specified, the default will be used.
        :param overrides: optional dictionary of inputs to override the defaults of the protocol.
        :param options: A dictionary of options that will be recursively set for the ``metadata.options`` input of all
            the ``CalcJobs`` that are nested in this work chain.
        :param sc_matrix: List of length 1 for supercell size.
        :param mu_spacing: Minimum distance in Angstrom between two starting muon positions  generated on a grid..
        :param kpoints_distance: the minimum desired distance in 1/Å between k-points in reciprocal space.
        :param charge_supercell: To run charged supercell for positive muon or not (neutral supercell).
        :param pseudo_family: the label of the pseudo family.
        :return: a process builder instance with all inputs defined ready for launch.
        """
        
        from aiida_quantumespresso.workflows.protocols.utils import recursive_merge
        import copy
        
        _overrides, start_mg_dict, structure = get_override_dict(structure, kpoints_distance, charge_supercell, magmom)
        
        overrides = recursive_merge(overrides,_overrides)
        print(overrides) 
        
        #### Musconv
        builder_musconv = MusconvWorkChain.get_builder_from_protocol(
                pw_code = pw_code,
                structure = structure,
                pseudo_family=pseudo_family,
                relax_unitcell=relax_musconv,
                )
        
        builder_musconv.pop('structure', None)
        
        #### simple PwBase for final scf mu-origin
        builder_pwscf = PwBaseWorkChain.get_builder_from_protocol(
                pw_code,
                structure,
                protocol=protocol,
                overrides=overrides.get("base",None),
                pseudo_family=pseudo_family,
                **kwargs,
                )
        
        
        #### PwRelax
        builder_relax = PwRelaxWorkChain.get_builder_from_protocol(
                pw_code,
                structure,
                protocol=protocol,
                overrides=overrides,
                pseudo_family=pseudo_family,
                relax_type=RelaxType.POSITIONS,
                **kwargs,
                )
        
        builder_relax.pop('structure', None)
        builder_relax.pop('base_final_scf', None)
        
        builder_pwscf['pw'].pop('structure', None)
        builder_pwscf.pop('kpoints_distance', None)       
        
        #### Builder
        builder = cls.get_builder()
        
        builder.structure = structure
        builder.pseudo_family = orm.Str(pseudo_family)
        
        #setting subworkflows inputs
        builder.musconv = builder_musconv  
        builder.pwscf = builder_pwscf
        builder.relax = builder_relax
        
        if not relax_musconv: builder.musconv.pop('relax')
        
        #useful to be used in overrides in the workflow. to be removed when new StructureData
        if start_mg_dict: 
            builder.magmom = magmom
            builder.mag_dict = start_mg_dict
        else:
            builder.pop('pwscf')
        
        #we can set this also wrt to some protocol, TOBE discussed
        if sc_matrix: 
            builder.sc_matrix=orm.List(sc_matrix)
            builder.pop('musconv')
        builder.mu_spacing=orm.Float(mu_spacing)
        builder.charge_supercell=orm.Bool(charge_supercell)
        builder.kpoints_distance = orm.Float(kpoints_distance)
        
        
        #MB: 
        # PpCalculation inputs: Only this, the rest is really default... 
        # I think is ok to be set on the fly later for now, but we can discuss.
        if pp_code: builder.pp_code = pp_code
        
        for i in ["pp_metadata","musconv_metadata","qe_settings"]:
            if hasattr(overrides,i):
                builder[i] = overrides[i]
        
        return builder

    #MB TODO
    def not_converged_supercell(self):
        """understand if musconv is needed: search for the sc_matrix in inputs."""
        if hasattr(self.inputs,"sc_matrix"):
            self.ctx.sc_matrix = self.inputs.sc_matrix[0]
                    
        return not hasattr(self.inputs,"sc_matrix")
        
    
    #MB TODO
    def converge_supercell(self):
        """call MusconvWorkchain"""

        inputs = AttributeDict(self.exposed_inputs(MusconvWorkChain, namespace='musconv'))
        inputs.structure = self.inputs.structure
        #inputs.pwscf.code = self.inputs.pw_code
        parameters_override = {
            "CONTROL": {
                "calculation": "scf",
                "restart_mode": "from_scratch",
                "tstress": True,
                "tprnfor": True,
            },
            "SYSTEM": {
                #"ecutwfc": 30.0,
                #"ecutrho": 240.0,
                "tot_charge": int(self.inputs.charge_supercell.value),
                #'nspin': 2,
                "occupations": "smearing",
                "smearing": "cold",
                "degauss": 0.01,
            },
            "ELECTRONS": {
                "conv_thr": 1.0e-6,
                "electron_maxstep": 300,
                "mixing_beta": 0.3,
            },
        }

        
        #
        
        if not "kpoints_distance" in inputs:
            inputs.kpoints_distance = self.inputs.kpoints_distance

        parameters = inputs.pwscf.pw.parameters.get_dict()
        
        parameters = recursive_merge(
            parameters, parameters_override
        )
        
        inputs.pwscf.pw.parameters = orm.Dict(dict=parameters)
        
        if hasattr(self.inputs,"musconv_metadata"):
            inputs.pwscf.pw.metadata = self.inputs.musconv_metadata

        inputs.metadata.call_link_label = f'musconvWorkchain'
        future = self.submit(MusconvWorkChain, **inputs)
        # key = f'workchains.sub{i_index}' #nested sub
        key = "MusconvWorkchain"
        self.report(
            f"Launching MusconvWorkchain (PK={future.pk}) for  structure {self.inputs.structure.get_pymatgen_structure().formula}"
        )
        self.to_context(**{key: future})

    #MB TODO
    def check_supercell_convergence(self):
        """check if is finished ok"""

        if not self.ctx["MusconvWorkchain"].is_finished_ok:
            return self.exit_codes.ERROR_MUSCONV_CALC_FAILED
        else:
            self.report("Found supercell")
            #see if relaxed unit cell is obtained. 
            self.ctx.sc_matrix = self.ctx["MusconvWorkchain"].outputs.Converged_SCmatrix.get_array('sc_mat')

            
    #I think here we should have some setup
    
    def setup(self):
        #just in case Musconv want also to provide the relaxed unit cell... maybe not necessary? 
        if not hasattr(self.ctx,"structure"): 
            self.ctx.structure = self.inputs.structure
            
        return
            

    def get_initial_muon_sites(self):
        """get list of starting muon sites"""

        # repharse Niche, input and outputs?
        # Not clear only spacing parameter, need for minimum number of initial muon?
        #self.ctx.structure. TODO
        
        self.ctx.mu_lst = niche_add_impurities(
            self.ctx.structure, orm.Str("H"), self.inputs.mu_spacing, orm.Float(1.0)
        )
        
        return

    def not_from_protocols(self):
        #does not check for Hubbard inputs, but anyway will change.
        return not "mag_dict" in self.inputs
    #MB this should be skipped now, as it is done in the protocols?
    def setup_magnetic_hubbardu_dict(self):
        """
        Gets:
        (i) Structure with kindname from magmom
        (ii) Dictionary for starting magnetization
        (iii) Dictionary for Hubbard-U parameters
        """
        '''
        Miki Bonacci: here there should be the improvement, - pt1 -
        only needed to be generated the structure data which contains all the magmom and hubbard info.
        Moreover, this should be done as protocol in the workflow? the hubbard U ecc...
        We have the magmom as input, then the Hubbard is added by default values, so we do not need to do it 
        inside the workflow. If we have HubbardStructureData, we can define it later (by inputs manually), or
        automatically in the get_builder_protocol. 
        Moreover, this can be done before the get_initial_muon sites: we do not define magmom and U for the muon.
        '''
        # get the magnetic kind relevant for pw spin-polarization setup
        if "magmom" in self.inputs:
            rst_mg = make_collinear_getmag_kind(
                self.inputs.structure, self.inputs.magmom
            )
            self.ctx.structure = rst_mg["struct_magkind"]
            self.ctx.start_mg_dict = rst_mg["start_mag_dict"]
        else:
            self.ctx.structure = self.inputs.structure

        # check and get hubbard u
        if self.inputs.qe.hubbard_u:
            inpt_st = self.ctx.structure.get_pymatgen_structure()
            rst_u = check_get_hubbard_u_parms(inpt_st)
            self.ctx.hubbardu_dict = rst_u
        else:
            self.ctx.hubbardu_dict = None

    def get_initial_supercell_structures(self):
        """Get initial supercell+muon list"""
        '''
        Miki Bonacci: here there should be the improvement, - pt2 -
        only needed to be generated the structure data which contains all the magmom and hubbard info.
        '''

        self.report("Getting supercell list")
        input_struct = self.ctx.structure.get_pymatgen_structure()
        muon_list = self.ctx.mu_lst

        supercell_list = gensup(input_struct, muon_list, self.ctx.sc_matrix)  # ordinary function
        if len(supercell_list) == 0:
            return self.exit_codes.ERROR_NO_SUPERCELLS
            
        self.ctx.supc_list = supercell_list

        # init relaxation calc count
        self.ctx.n = 0
        self.ctx.n_uuid_dict = {}

    def setup_pw_overrides(self):
        """Get the required overrides i.e pw parameter setup. NOT INCLUDED IN THE OUTLINE"""
        '''
        Miki Bonacci: I think that this overrides are no more needed once we have the MagneticStructureData.
        Also, if we do this in a protocol, we can also tune it before the run, just in case.
        Hubbard can be set by protocol, as we have the defaults. 
        base_final_scf not needed because it is not currently used: but we can use its inputs to run the final scf with muon at the orgin? 
        '''
        self.report("Setting up the relaxation calculation")
        overrides = {
            #'final_scf' : orm.Bool(False),
            "base": {
                "kpoints_distance": orm.Float(self.inputs.kpoints_distance.value),
                "pseudo_family":self.inputs.pseudo_family,
                "pw": {
                    "parameters": {},
                    "metadata": {},
                },
            },
            #"base_final_scf": {
            #    "pseudo_family": self.inputs.pseudo_family.value,
            #},
            "clean_workdir": orm.Bool(True),
        }

        ##TO DO:put a check on  parameters that cannot be set by hand in the overrides eg mag, hubbard

        # set some cards
        overrides["base"]["pw"]["parameters"] = recursive_merge(
            overrides["base"]["pw"]["parameters"], {"CONTROL": {"nstep": 200}}
        )
        # overrides['base']['pw']['parameters'] = recursive_merge(overrides['base']['pw']['parameters'], {'SYSTEM':{'smearing': 'gaussian'}})
        overrides["base"]["pw"]["parameters"] = recursive_merge(
            overrides["base"]["pw"]["parameters"],
            {"ELECTRONS": {"electron_maxstep": 300}},
        )
        overrides["base"]["pw"]["parameters"] = recursive_merge(
            overrides["base"]["pw"]["parameters"], {"ELECTRONS": {"mixing_beta": 0.30}}
        )
        # overrides['base']['pw']['parameters'] = recursive_merge(overrides['base']['pw']['parameters'], {'ELECTRONS':{'conv_thr': 1.0e-6}})
        overrides["base"]["pw"]["metadata"] = recursive_merge(
            overrides["base"]["pw"]["metadata"],
            {
                "description": "Muon site calculations for "
                + self.inputs.structure.get_pymatgen_structure().formula
            },
        )
        if self.inputs.charge_supercell:
        #
        # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'CONTROL':{'nstep': 200}})
        # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'smearing': 'gaussian'}})
        # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'ELECTRONS':{'electron_maxstep': 300}})
        # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'ELECTRONS':{'mixing_beta': 0.30}})
        # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'ELECTRONS':{'conv_thr': 1.0e-6}})
        # overrides['base_final_scf']['pw']['metadata'] = recursive_merge(overrides['base_final_scf']['pw']['metadata'], {'description': 'Muon site calculations for '+self.inputs.structure.get_pymatgen_structure().formula})

            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"], {"SYSTEM": {"tot_charge": 1.0}}
            )
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'tot_charge': 1.0}})

        # if self.inputs.magmom is not None:
        #MB this should be automatically done in the new implementation with the MagneticStructureData.
        if "magmom" in self.inputs and self.ctx.start_mg_dict:
            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"], {"SYSTEM": {"nspin": 2}}
            )
            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"],
                {
                    "SYSTEM": {
                        "starting_magnetization": self.ctx.start_mg_dict.get_dict()
                    }
                },
            )
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'nspin': 2}})
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'starting_magnetization': self.ctx.start_mg_dict.get_dict()}})

        # check and assign hubbard u
        # MB this should be automatically done in the new implementation, at least at the protocol generation level.
        if "hubbardu_dict" in self.ctx:
            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"], {"SYSTEM": {"lda_plus_u": True}}
            )
            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"],
                {"SYSTEM": {"lda_plus_u_kind": 0}},
            )
            overrides["base"]["pw"]["parameters"] = recursive_merge(
                overrides["base"]["pw"]["parameters"],
                {"SYSTEM": {"Hubbard_U": self.ctx.hubbardu_dict}},
            )
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'lda_plus_u': True}})
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'lda_plus_u_kind': 0}})
            # overrides['base_final_scf']['pw']['parameters'] = recursive_merge(overrides['base_final_scf']['pw']['parameters'], {'SYSTEM':{'Hubbard_U': self.ctx.hubbardu_dict}})


        self.ctx.overrides = overrides

    def compute_supercell_structures(self):
        """Run relax workflows for each muon supercell"""

        self.report("Computing muon supercells")
        supercell_list = self.ctx.supc_list
        
        inputs = AttributeDict(self.exposed_inputs(PwRelaxWorkChain, namespace='relax'))

        if "overrides" in self.ctx:            
            """ 
            This does not work: inputs = recursive_merge(inputs["base"], self.ctx.overrides["base"])
            so I'm gonna do this:
            """
            inputs = recursive_merge(self.ctx.overrides,inputs)
            inputs["clean_workdir"] = self.ctx.overrides.pop("clean_workdir",orm.Bool(True))
            inputs = prepare_process_inputs(PwRelaxWorkChain, inputs)
            
        if not "kpoints_distance" in inputs.base:
            inputs.base.kpoints_distance = self.inputs.kpoints_distance
        
        for i_index in range(len(supercell_list)):
                        
            inputs.structure = orm.StructureData(pymatgen=supercell_list[i_index])
            
            #MB: this should be done once for all, put here just for convenience
            inputs.base.pw.pseudos = get_pseudos(
            inputs.structure, self.inputs.pseudo_family.value
            )
            
            # No final scf in base. !MB: Here is set empty, so we can store in the inputs the info for the scf-mu-origin
            #inputs.base_final_scf = {}

            # Set the `CALL` link label
            inputs.metadata.call_link_label = f'supercell_{i_index:02d}'
            
            future = self.submit(PwRelaxWorkChain, **inputs)
            # key = f'workchains.sub{i_index}' #nested sub
            key = f"workchain_{i_index}"
            self.report(
                f"Launching PwRelaxWorkChain (PK={future.pk}) for supercell structure {supercell_list[i_index].formula} with index {i_index}"
            )
            self.to_context(**{key: future})

    # work tomorrow
    def collect_relaxed_structures(self):
        """Retrieve final positions and energy from the relaxed structures"""

        self.report("Gathering computed positions and energy")
        supercell_list = self.ctx.supc_list

        computed_results = []

        # for nested
        # for key, workchain in self.ctx.workchains.items():
        #    if not workchain.is_finished_ok

        n_notf = 0
        for i_index in range(len(supercell_list)):
            key = f"workchain_{i_index}"
            workchain = self.ctx[key]

            # TODO:IMPLEMEMENT CHECKS FOR RESTART OF UNFINISHED CALCULATION
            #     AND/OR NUMBER OF UNCONVERGED CALC IS ACCEPTABLE

            if not workchain.is_finished_ok:
                self.report(
                    f"PwRelaxWorkChain failed with exit status {workchain.exit_status}"
                )
                n_notf += 1
                # if failed calculation is more than 40%, then exit
                if float(n_notf) / len(supercell_list) > 0.4:
                    return self.exit_codes.ERROR_RELAX_CALC_FAILED
            else:
                self.ctx.n += 1
                uuid = workchain.uuid
                energy = workchain.outputs.output_parameters.get_dict()["energy"]
                rlx_structure = (
                    workchain.outputs.output_structure.get_pymatgen_structure()
                )
                # rlx_structure = workchain.outputs.output_structure

                # computed_results.append((pk,rlx_structure,energy))
                computed_results.append(
                    (
                        {
                            "idx": self.ctx.n,
                            "rlxd_struct": rlx_structure.as_dict(),
                            "energy": energy,
                        }
                    )
                )
                self.ctx.n_uuid_dict.update({self.ctx.n: uuid})

                # print(computed_results)

        self.ctx.relaxed_outputs = computed_results

    def analyze_relaxed_structures(self):
        """
        Analyze relaxed structures and get unique candidate sites and
        check if there are new magnetic equivalent sites to calculate
        """
        self.report("Analyzing the relaxed structures")
        inpt_st = self.ctx.structure.get_pymatgen_structure()

        if "magmom" in self.inputs:
            r_anly = analyze_structures(
                self.ctx.supc_list[0],
                self.ctx.relaxed_outputs,
                inpt_st,
                self.inputs.magmom,
            )
        else:
            r_anly = analyze_structures(
                self.ctx.supc_list[0], self.ctx.relaxed_outputs, inpt_st
            )

        self.ctx.unique_cluster = r_anly["unique_pos"]
        # print('uniq_positions',self.ctx.unique_cluster)

        # revisit, this so the initial inputs and collected results are not ovewritten with repeated calls in outline
        self.ctx.supc_list_all = self.ctx.supc_list
        self.ctx.relaxed_outputs_all = self.ctx.relaxed_outputs

        self.ctx.supc_list = r_anly["mag_inequivalent"]

    def new_struct_after_analyze(self):
        """Check if there is new magnetic inequivalent sites"""
        self.report("Checking new structures to calculate")

        return len(self.ctx.supc_list) > 0

    def collect_all_results(self):
        """Collecting results of new structures and then append"""
        self.report("Appending results of new structures ")

        self.ctx.relaxed_outputs_all.extend(self.ctx.relaxed_outputs)
        self.ctx.unique_cluster.extend(self.ctx.relaxed_outputs)

    def structure_is_magnetic(self):
        """Checking if structure is magnetic"""
        self.report("Checking if structure is magnetic ")

        # return self.inputs.magmom is not None
        # return 'magmom' in self.inputs
        if "magmom" in self.inputs:
            return self.inputs.magmom is not None
        else:
            return False

    # scf first then pp.x ! TODO: NOT NECESSARY REMOVE ON REVISIT

    def run_final_scf_mu_origin(self):
        """Move muon to origin and  perform scf"""
        unique_cluster_list = self.ctx.unique_cluster
        
        inputs = AttributeDict(self.exposed_inputs(PwBaseWorkChain, namespace='pwscf'))
        
        if "overrides" in self.ctx:
            
            """ 
            This does not work: inputs = recursive_merge(inputs["base"], self.ctx.overrides["base"])
            so I'm gonna do this:
            """
            
            inputs = recursive_merge(self.ctx.overrides["base"],inputs)
            inputs["clean_workdir"] = self.ctx.overrides.pop("clean_workdir",orm.Bool(False))
            inputs = prepare_process_inputs(PwBaseWorkChain, inputs)
            
        if not "kpoints_distance" in inputs:
            inputs.kpoints_distance = self.inputs.kpoints_distance
        
        for j_index, clus in enumerate(unique_cluster_list):
            #
            # rlx_st = clus['rlxd_struct']
            # rlx_struct = orm.StructureData(pymatgen = rlx_st)
            # or
            c_uuid = self.ctx.n_uuid_dict[clus["idx"]]
            rlx_node = orm.load_node(c_uuid)
            rlx_st = rlx_node.outputs.output_structure.get_pymatgen_structure()

            # move muon to origin
            musite = rlx_st.frac_coords[rlx_st.atomic_numbers.index(1)]
            rlx_st.translate_sites(
                range(rlx_st.num_sites), -musite, frac_coords=True, to_unit_cell=False
            )
            inputs.pw.structure = orm.StructureData(pymatgen=rlx_st)
            
            #MB: this should be done once for all, put here just for convenience
            inputs.pw.pseudos = get_pseudos(
            inputs.pw.structure, self.inputs.pseudo_family.value
            )
            
            # Set the `CALL` link label
            inputs.metadata.call_link_label = f'mu_origin_supercell_{j_index:02d}'
            
            pwb_future = self.submit(PwBaseWorkChain, **inputs)
            pwb_key = f"pwb_workchain_{j_index}"
            self.report(
                f"Launching PwBaseWorkChain (PK={pwb_future.pk}) for PWRelaxed (uuid={c_uuid}) structure"
            )
            self.to_context(**{pwb_key: pwb_future})

    def compute_spin_density(self):
        """compute spin density at unique candidate sites"""
        self.report("Computing Spin density")

        PpCalculation = CalculationFactory("quantumespresso.pp")
        pp_builder = PpCalculation.get_builder()
        pp_builder.code = self.inputs.pp_code

        #MB: if "pp.metadata" in self.inputs:
        if hasattr(self.inputs,"pp_metadata"):
            pp_builder.metadata = self.inputs.pp_metadata.get_dict()


        parameters = orm.Dict(
            dict={
                "INPUTPP": {
                    "plot_num": 6,
                },
                "PLOT": {"iflag": 3},
            }
        )
        pp_builder.parameters = parameters

        unique_cluster_list = self.ctx.unique_cluster

        # for direct pp.x without scf
        """
        for j_index, clus in enumerate(unique_cluster_list):
            c_uuid = self.ctx.n_uuid_dict[clus['idx']]
            rlx_node = orm.load_node(c_uuid)
            pp_builder.parent_folder = rlx_node.outputs.remote_folder

            pp_future = self.submit(pp_builder)
            pkey = f'pworkchain_{j_index}'
            self.report(f'Launching PpCalcJOb  with (PK={pp_future.pk}) for PWRelaxed (UUID={c_uuid}) structure')
            self.to_context(**{pkey: pp_future})
        """

        # inspect the scf pw.x run and then run pp.x
        for j_index, clus in enumerate(unique_cluster_list):
            pwb_key = f"pwb_workchain_{j_index}"
            pwb_workchain = self.ctx[pwb_key]

            if not pwb_workchain.is_finished_ok:
                self.report(
                    f"PwbaseWorkChain failed with exit status {pwb_workchain.exit_status}"
                )
                return self.exit_codes.ERROR_BASE_CALC_FAILED
            else:
                pp_builder.parent_folder = pwb_workchain.outputs.remote_folder
                # print('pbasepk',pwb_workchain.pk)

                pp_future = self.submit(pp_builder)
                pkey = f"pworkchain_{j_index}"
                c_uuid = self.ctx.n_uuid_dict[clus["idx"]]
                self.report(
                    f"Launching PpCalcJOb  with (PK={pp_future.pk}) for PWRelaxed \
                (UUID={c_uuid}) structure and PWBase-mu-origin (PK={pwb_workchain.pk}) "
                )
                self.to_context(**{pkey: pp_future})

    def inspect_get_contact_hyperfine(self):
        """compute spin density at unique candidate sites"""
        self.report("Getting Contact field")
        unique_cluster_list = self.ctx.unique_cluster
        # contact_hf = []
        chf_dict = {}

        for j_index, clus in enumerate(unique_cluster_list):
            pwb_key = f"pwb_workchain_{j_index}"  # remove later
            pwb_workchain = self.ctx[pwb_key]

            pkey = f"pworkchain_{j_index}"
            pworkchain = self.ctx[pkey]

            if not pworkchain.is_finished_ok:
                self.report(
                    f"PpWorkChain failed with exit status {pworkchain.exit_status}"
                )
                return self.exit_codes.ERROR_PP_CALC_FAILED
            else:
                p_pk = pworkchain.pk
                sp_density = pworkchain.outputs.output_data.get_array("data")[0, 0, 0]
                # contact_hf.append(({'rlx_idx':clus['idx'],'pwb_pk':pwb_workchain.pk, 'pp_pk':pworkchain.pk, 'spin_density':sp_density, 'hf_T':sp_density*52.430351})) # In Tesla
                chf_dict.update(
                    {str(clus["idx"]): [sp_density, sp_density * 52.430351]}
                )

        # self.ctx.cont_hf = contact_hf
        self.ctx.cont_hf = orm.Dict(dict=chf_dict)
        # print("contact_results ",chf_dict)

    def get_dipolar_field(self):
        unique_cluster_list = self.ctx.unique_cluster
        cnt_field_dict = self.ctx.cont_hf.get_dict()
        dip_results = []
        for j_index, clus in enumerate(unique_cluster_list):
            #
            # rlx_st = clus['rlxd_struct']
            rlx_st = Structure.from_dict(clus["rlxd_struct"])
            rlx_struct = orm.StructureData(pymatgen=rlx_st)
            cnt_field = cnt_field_dict[str(clus["idx"])][1]
            print(cnt_field)
            b_field = compute_dipolar_field(
                self.inputs.structure,
                self.inputs.magmom,
                self.inputs.sc_matrix[0],
                rlx_struct,
                orm.Float(cnt_field),
            )
            # dip_results.update({str(clus['idx']):[b_field[0][0], b_field[0][1], b_field[0][2]]})  #as dict
            dip_results.append(
                (
                    {
                        "idx": clus["idx"],
                        "Bdip": b_field[0][0],
                        "B_T": b_field[0][1],
                        "B_T_norm": b_field[0][2],
                    }
                )
            )

        self.ctx.dipolar_dict = orm.List(dip_results)
        print("dipolar_results ", dip_results)

    def set_hyperfine_outputs(self):
        """outputs"""
        self.report("Setting hypferfine Outputs")
        # self.out('unique_sites_hyperfine', get_list(self.ctx.cont_hf))
        self.out("unique_sites_hyperfine", self.ctx.cont_hf)
        self.out("unique_sites_dipolar", self.ctx.dipolar_dict)

    def set_relaxed_muon_outputs(self):
        """outputs"""
        # self.report('Setting Relaxation and analysis Outputs')

        self.out(
            "all_index_uuid",
            get_dict_uuid(orm.List(list(self.ctx.n_uuid_dict.items()))),
        )

        self.out("all_sites", get_dict_output(orm.List(self.ctx.relaxed_outputs_all)))

        self.out("unique_sites", get_dict_output(orm.List(self.ctx.unique_cluster)))
        
        self.report("final output provided, the workflow is completed successfully.")


#################################################################################
# calcfunctions and called functions

def get_pseudos(aiida_struc, pseudofamily):
    """Get pseudos"""
    family = orm.load_group(pseudofamily)
    pseudos = family.get_pseudos(structure=aiida_struc)
    return pseudos

@calcfunction
def get_dict_uuid(outdata):
    """convert list to aiida dictionary for outputting"""
    out_dict = {}

    for i, dd in enumerate(outdata):
        out_dict.update({str(dd[0]): dd[1]})

    return orm.Dict(dict=out_dict)


@calcfunction
def get_dict_output(outdata):
    """convert list to aiida dictionary for outputting"""
    out_dict = {}

    for i, dd in enumerate(outdata):
        out_dict.update({str(dd["idx"]): [dd["rlxd_struct"], dd["energy"]]})

    return orm.Dict(dict=out_dict)


@calcfunction
def niche_add_impurities(
    structure: orm.StructureData,
    niche_atom: orm.Str,
    niche_spacing: orm.Float,
    niche_distance: orm.Float,
):
    """
    This calcfunction calls Niche. Supplies structure, atom index and impurity
    spacing required to get the grid initial sites

    Return: Adapted here to return only lists of generated muon sites.
    """

    # niche_class = get_object_from_string("niche.Niche")

    pmg_st = structure.get_pymatgen_structure()
    # niche_instance = niche_class(pmg_st, niche_atom.value)
    niche_instance = Niche(pmg_st, niche_atom.value)

    n_st = niche_instance.apply(niche_spacing.value, niche_distance.value)

    '''
    Miki Bonacci: The new_structure_data is needed here? don't think so.
    '''
    new_structure_data = orm.StructureData()
    new_structure_data.set_pymatgen(n_st)
    # print(n_st)

    '''
    Miki Bonacci: I think the muon symmetry beaking (see next lines) can be optimized.
    '''
    # +0.001 to break symmetry if at symmetry pos
    mu_lst = [
        i + 0.001
        for j, i in enumerate(n_st.frac_coords)
        if n_st.species[j].value == niche_atom.value
    ]

    # mu_lst_node = orm.ArrayData()
    # mu_lst_node.set_array('mu_list', np.array(mu_lst))

    # return new_structure_data
    # return {"mu_lst":mu_lst_node}
    return orm.List(mu_lst)


# @calcfunction
# def gensup(aiida_struc, mu_list, sc_matrix):
# p_st = aiida_struc.get_pymatgen_structure()
# imp_list = mu_list.get_array('mu_list')
# sc_mat = sc_matrix.get_array('sc_matrix')


# Do we really need to keep this in the provenance?
'''
Miki Bonacci: I think yes, so we can track the provenance to the starting StructureData? 
'''
def gensup(p_st, mu_list, sc_mat):
    """
    This makes the supercell with the given SC matrix.
    It also appends the muon.

    Returns: list of supercell structures with muon.
              Number of supercells depends on number of imput mulist
    """
    supc_list = []
    for ij in mu_list:
        p_scst = p_st.copy()
        p_scst.make_supercell(sc_mat)
        ij_sc = (np.dot(ij, np.linalg.inv(sc_mat))) % 1
        # ij_sc = [x + 0.001 for x in ij_sc]
        p_scst.append(
            species="H",
            coords=ij_sc,
            coords_are_cartesian=False,
            validate_proximity=True,
            properties={"kind_name": "H"},
        )
        supc_list.append(p_scst)
    return supc_list


@calcfunction
def make_collinear_getmag_kind(aiid_st, magmm):
    """
    This calls the 'get_collinear_mag_kindname' utility function.
    It takes the provided magmom, make it collinear and then with
    assign kind_name property for each atom site relevant
    spin polarized calculation.

    Returns: Structure data and dictionary of pw starting magnetization card.
    """
    p_st = aiid_st.get_pymatgen_structure()
    # magmm = magmom_node.get_array('magmom')
    # from array to Magmom object
    magmoms = [Magmom(magmom) for magmom in magmm]

    st_k, st_m_dict = get_collinear_mag_kindname(p_st, magmoms)

    aiida_st2 = orm.StructureData(pymatgen=st_k)
    aiid_dict = orm.Dict(dict=st_m_dict)

    return {"struct_magkind": aiida_st2, "start_mag_dict": aiid_dict}


def analyze_structures(init_supc, rlxd_results, input_st, magmom=None):
    """
    This calls "cluster_unique_sites" function that analyzes and clusters
    the relaxed muon positions.

    Returns:
    (i) List of relaxed unique candidate sites supercell structures
    (ii) List of to be calculated magnetic inequivalent supercell structures
    """
    idx_lst, mu_lst, enrg_lst = load_workchain_data(rlxd_results)

    if magmom:
        assert input_st.num_sites == len(magmom)
        st_smag = input_st.copy()
        for i, m in enumerate(magmom):
            st_smag[i].properties["magmom"] = Magmom(m)
    else:
        st_smag = input_st.copy()

    clus_pos, new_pos = cluster_unique_sites(
        idx_lst, mu_lst, enrg_lst, p_st=input_st, p_smag=st_smag
    )

    # REVISIT
    # TODO-clean: lines below can go in the function 'cluster_unique_sites' with much less lines.

    # get input supercell structure with distortions of new mag inequivalent position
    nw_stc_calc = []
    if len(new_pos) > 0:
        for i, nwp in enumerate(new_pos):
            for j, d in enumerate(rlxd_results):
                if nwp[0] == d["idx"]:
                    nw_st = get_struct_wt_distortions(
                        init_supc, d["rlxd_struct"], nwp[1], st_smag
                    )
                    nw_stc_calc.append(nw_st)

    uniq_clus_pos = []
    for i, clus in enumerate(clus_pos):
        for j, d in enumerate(rlxd_results):
            if clus[0] == d["idx"]:
                uniq_clus_pos.append(d)

    assert len(clus_pos) == len(uniq_clus_pos)

    return {"unique_pos": uniq_clus_pos, "mag_inequivalent": nw_stc_calc}


@calcfunction
def compute_dipolar_field(
    p_st: orm.StructureData,
    magmm: orm.List,
    sc_matr: orm.List,
    r_supst: orm.StructureData,
    cnt_field: orm.Float,
):
    """
    This calcfunction calls the compute dipolar field
    """

    pmg_st = p_st.get_pymatgen_structure()
    r_sup = r_supst.get_pymatgen_structure()

    b_fld = compute_dip_field(pmg_st, magmm, sc_matr, r_sup, cnt_field.value)

    return orm.List([b_fld])


#Creates the _overrides used in the protocols and in the forcing inputs step.
def get_override_dict(structure, kpoints_distance, charge_supercell,magmom):
    _overrides = {
            "base": {
                "kpoints_distance": kpoints_distance,
                "pw": {
                    "parameters": {
                "CONTROL": {
                    "nstep": 200
                    },
                "SYSTEM":{
                    "occupations": "smearing",
                    "smearing": "gaussian",
                    "degauss": 0.01,},
                "ELECTRONS": {
                    "electron_maxstep": 300,
                    "mixing_beta": 0.30,
                },
                },
                    "metadata": {
                    "description": "Muon site calculations for "
                    + structure.get_pymatgen_structure().formula
                },
                },
            },
            "base_final_scf": {},
            "clean_workdir": orm.Bool(True),
        }

    if charge_supercell:
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["tot_charge"] = 1.0
        
    # MAGMOMS       
    if magmom:
        rst_mg = make_collinear_getmag_kind(
            structure, magmom,
        )
        structure = rst_mg["struct_magkind"]
        start_mg_dict = rst_mg["start_mag_dict"]
        
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["nspin"]= 2
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["starting_magnetization"] = start_mg_dict.get_dict()
        
    else:
        start_mg_dict = None
    # HUBBARD
    # check and assign hubbard u
    inpt_st = structure.get_pymatgen_structure()
    ##TO DO:put a check on  parameters that cannot be set by hand in the overrides eg mag, hubbard.
    rst_u = check_get_hubbard_u_parms(inpt_st)
    hubbardu_dict = rst_u 
    if hubbardu_dict:
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["lda_plus_u"] = True
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["lda_plus_u_kind"] = 0
        _overrides["base"]["pw"]["parameters"]["SYSTEM"]["Hubbard_U"] = hubbardu_dict
        
    return _overrides, start_mg_dict, structure


def iterdict(d,key):
  value = None
  for k,v in d.items():
    if isinstance(v, dict):
        value = iterdict(v,key)
    else:            
        if k == key:
          return v
    if value: return value


def recursive_consistency_check(input_dict,_):
    import copy
    
    """Validation of the inputs provided for the FindMuonWorkChain.
    """
    
    parameters = copy.deepcopy(input_dict)
    _overrides, start_mg_dict, structure = get_override_dict(parameters["structure"], parameters["kpoints_distance"], parameters["charge_supercell"],parameters.pop('magmom',None))
    
    keys = ["tot_charge","nspin","occupations","smearing"]
    
    wrong_inputs_relax = []
    wrong_inputs_pwscf = []
    
    musconv_inconsistency = ''
    if "musconv" in parameters:
        musconv_inconsistency = musconv_input_validator(parameters["musconv"])
    
    inconsistency_sentence = musconv_inconsistency
    
    if parameters["relax"]["base"]["pw"]["parameters"].get_dict()["CONTROL"]["calculation"] != 'relax':
        inconsistency_sentence+=f'Checking inputs.relax.base.pw.parameters.CONTROL.calculation: can be only "relax". No cell relaxation should be performed.'
    
    
    if 'base_final_scf' in parameters['relax']:
        if parameters['relax']['base_final_scf'] ==  {'metadata': {}, 'pw': {'metadata': {'options': {'stash': {}}}, 'monitors': {}, 'pseudos': {}}}:
            pass
        elif parameters['relax']['base_final_scf'] ==  {}:
            pass
        else:
            inconsistency_sentence+=f'Checking inputs.relax.base_final_scf: should not be set, the final scf after relaxation is not supported in the FindMuonWorkChain.'
    
    if "pwscf" in parameters: #mu scf origin.
        if not "pp_code" in parameters: 
            inconsistency_sentence+=f'Checking inputs: "pp_code" input not provided but required!'
        elif not parameters["pp_code"]: 
            inconsistency_sentence+=f'Checking inputs: "pp_code" input not provided but required!'
        
    for key in keys:
        value_input_relax = iterdict(parameters["relax"]["base"]["pw"]["parameters"].get_dict(),key)
        value_overrides = iterdict(_overrides,key)
        #print(value_input_relax,value_input_pwscf,value_overrides)
        if value_input_relax != value_overrides:
            if value_input_relax in [0, None] and value_overrides in [0, None]:
                continue # 0 is None and viceversa
            wrong_inputs_relax.append(key)
            inconsistency_sentence += f'Checking inputs.relax.base.pw.parameters input: "{key}" is not correct. You provided the value "{value_input_relax}", but only "{value_overrides}" is consistent with your settings.\n'
        
        if "pwscf" in parameters: #mu scf origin.
            value_input_pwscf = iterdict(parameters["pwscf"]["pw"]["parameters"].get_dict(),key)
            if value_input_pwscf != value_overrides:
                if key == "nspin" and value_input_pwscf == 2: 
                    continue
                if value_input_pwscf in [0, None] and value_overrides in [0, None]:
                    continue # 0 is None and viceversa
                wrong_inputs_pwscf.append(key)
                inconsistency_sentence += f'Checking inputs.pwscf.pw.parameters input: "{key}" is not correct. You provided the value "{value_input_pwscf}", but only "{value_overrides}" is consistent with your settings.\n'
    
    if len(wrong_inputs_relax+wrong_inputs_pwscf)>0:
        raise ValueError('\n'+inconsistency_sentence+'\n Please check the inputs of your FindMuonWorkChain instance or use "get_builder_from_protocol()" method to populate correctly the inputs.')

                      
    return 
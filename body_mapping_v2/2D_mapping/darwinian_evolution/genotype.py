"""Genotype for a modular robot body and brain."""

from dataclasses import dataclass
from random import Random
from typing import List

import multineat
import sqlalchemy
from revolve2.core.database import IncompatibleError, Serializer
from revolve2.core.modular_robot import ModularRobot
from revolve2.genotypes.cppnwin import Genotype as CppnwinGenotype
from revolve2.genotypes.cppnwin import GenotypeSerializer as CppnwinGenotypeSerializer
from revolve2.genotypes.cppnwin import crossover_v1, mutate_v1
from revolve2.core.database.serializers import FloatSerializer
from body_genotype_v2 import random_v1 as body_random
from revolve2.core.modular_robot.brains import (
    BrainCpgNetworkStatic, make_cpg_network_structure_neighbour)
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.future import select

from array_genotype.array_genotype import ArrayGenotype, ArrayGenotypeSerializer, random_v1 as random_array_genotype
from array_genotype.array_genotype_mutation import mutate as brain_mutation
from array_genotype.array_genotype_crossover import crossover as brain_crossover


def _make_multineat_params() -> multineat.Parameters:
    multineat_params = multineat.Parameters()

    multineat_params.MutateRemLinkProb = 0.02
    multineat_params.RecurrentProb = 0.0
    multineat_params.OverallMutationRate = 0.15
    multineat_params.MutateAddLinkProb = 0.08
    multineat_params.MutateAddNeuronProb = 0.01
    multineat_params.MutateWeightsProb = 0.90
    multineat_params.MaxWeight = 8.0
    multineat_params.WeightMutationMaxPower = 0.2
    multineat_params.WeightReplacementMaxPower = 1.0
    multineat_params.MutateActivationAProb = 0.0
    multineat_params.ActivationAMutationMaxPower = 0.5
    multineat_params.MinActivationA = 0.05
    multineat_params.MaxActivationA = 6.0

    multineat_params.MutateNeuronActivationTypeProb = 0.03

    multineat_params.MutateOutputActivationFunction = False

    multineat_params.ActivationFunction_SignedSigmoid_Prob = 0.0
    multineat_params.ActivationFunction_UnsignedSigmoid_Prob = 0.0
    multineat_params.ActivationFunction_Tanh_Prob = 1.0
    multineat_params.ActivationFunction_TanhCubic_Prob = 0.0
    multineat_params.ActivationFunction_SignedStep_Prob = 1.0
    multineat_params.ActivationFunction_UnsignedStep_Prob = 0.0
    multineat_params.ActivationFunction_SignedGauss_Prob = 1.0
    multineat_params.ActivationFunction_UnsignedGauss_Prob = 0.0
    multineat_params.ActivationFunction_Abs_Prob = 0.0
    multineat_params.ActivationFunction_SignedSine_Prob = 1.0
    multineat_params.ActivationFunction_UnsignedSine_Prob = 0.0
    multineat_params.ActivationFunction_Linear_Prob = 1.0

    multineat_params.MutateNeuronTraitsProb = 0.0
    multineat_params.MutateLinkTraitsProb = 0.0

    multineat_params.AllowLoops = False

    return multineat_params


_MULTINEAT_PARAMS = _make_multineat_params()


@dataclass
class Genotype:
    """Genotype for a modular robot."""

    body: CppnwinGenotype
    brain: ArrayGenotype
    random_seed: int


class GenotypeSerializer(Serializer[Genotype]):
    """Serializer for storing modular robot genotypes."""

    @classmethod
    async def create_tables(cls, session: AsyncSession) -> None:
        """
        Create all tables required for serialization.

        This function commits. TODO fix this
        :param session: Database session used for creating the tables.
        """
        await (await session.connection()).run_sync(DbBase.metadata.create_all)
        await CppnwinGenotypeSerializer.create_tables(session)
        await ArrayGenotypeSerializer.create_tables(session)
        await FloatSerializer.create_tables(session)

    @classmethod
    def identifying_table(cls) -> str:
        """
        Get the name of the primary table used for storage.

        :returns: The name of the primary table.
        """
        return DbGenotype.__tablename__

    @classmethod
    async def to_database(
        cls, session: AsyncSession, objects: List[Genotype]
    ) -> List[int]:
        """
        Serialize the provided objects to a database using the provided session.

        :param session: Session used when serializing to the database. This session will not be committed by this function.
        :param objects: The objects to serialize.
        :returns: A list of ids to identify each serialized object.
        """
        body_ids = await CppnwinGenotypeSerializer.to_database(
            session, [o.body for o in objects]
        )
        brain_ids = await ArrayGenotypeSerializer.to_database(
            session, [o.brain for o in objects]
        )

        seed_ids = await FloatSerializer.to_database(
            session, [o.random_seed for o in objects]
        )

        dbgenotypes = [
            DbGenotype(body_id=body_id, brain_id=brain_id, seed_id=seed_id)
            for body_id, brain_id, seed_id in zip(body_ids, brain_ids, seed_ids)
        ]

        session.add_all(dbgenotypes)
        await session.flush()
        ids = [
            dbfitness.id for dbfitness in dbgenotypes if dbfitness.id is not None
        ]  # cannot be none because not nullable. check if only there to silence mypy.
        assert len(ids) == len(objects)  # but check just to be sure
        return ids

    @classmethod
    async def from_database(
        cls, session: AsyncSession, ids: List[int]
    ) -> List[Genotype]:
        """
        Deserialize a list of objects from a database using the provided session.

        :param session: Session used for deserialization from the database. No changes are made to the database.
        :param ids: Ids identifying the objects to deserialize.
        :returns: The deserialized objects.
        :raises IncompatibleError: In case the database is not compatible with this serializer.
        """
        rows = (
            (await session.execute(select(DbGenotype).filter(DbGenotype.id.in_(ids))))
            .scalars()
            .all()
        )

        if len(rows) != len(ids):
            raise IncompatibleError()

        id_map = {t.id: t for t in rows}
        body_ids = [id_map[id].body_id for id in ids]
        brain_ids = [id_map[id].brain_id for id in ids]
        seed_ids = [id_map[id].seed_id for id in ids]

        body_genotypes = await CppnwinGenotypeSerializer.from_database(
            session, body_ids
        )
        brain_genotypes = await ArrayGenotypeSerializer.from_database(
            session, brain_ids
        )

        random_seeds = await FloatSerializer.from_database(
            session, seed_ids
        )

        genotypes = [
            Genotype(body, brain, seed)
            for body, brain, seed in zip(body_genotypes, brain_genotypes, random_seeds)
        ]

        return genotypes


def random(
    innov_db_body: multineat.InnovationDatabase,
    rng: Random,
    num_initial_mutations: int,
    robot_grid_size: int,
) -> Genotype:
    """
    Create a random genotype.

    :param innov_db_body: Multineat innovation database for the body. See Multineat library.
    :param innov_db_brain: Multineat innovation database for the brain. See Multineat library.
    :param rng: Random number generator.
    :param num_initial_mutations: The number of times to mutate to create a random network. See CPPNWIN genotype.
    :returns: The created genotype.
    """
    multineat_rng = _multineat_rng_from_random(rng)

    body = body_random(
        innov_db_body,
        multineat_rng,
        _MULTINEAT_PARAMS,
        multineat.ActivationFunction.TANH,
        num_initial_mutations,
    )

    brain = random_array_genotype(robot_grid_size, rng)

    random_seed = rng.randint(1, 10000)

    return Genotype(body, brain, random_seed)


def mutate(
    genotype: Genotype,
    innov_db_body: multineat.InnovationDatabase,
    innov_db_brain: multineat.InnovationDatabase,
    rng: Random,
) -> Genotype:
    """
    Mutate a genotype.

    The genotype will not be changed; a mutated copy will be returned.

    :param genotype: The genotype to mutate. This object is not altered.
    :param innov_db_body: Multineat innovation database for the body. See Multineat library.
    :param innov_db_brain: Multineat innovation database for the brain. See Multineat library.
    :param rng: Random number generator.
    :returns: A mutated copy of the provided genotype.
    """
    multineat_rng = _multineat_rng_from_random(rng)

    return Genotype(
        mutate_v1(genotype.body, _MULTINEAT_PARAMS, innov_db_body, multineat_rng),
        brain_mutation(genotype.brain, 0, 0.5, 0.8),
        genotype.random_seed
    )


def crossover(
    parent1: Genotype,
    parent2: Genotype,
    rng: Random,
    first_best: bool
) -> Genotype:
    """
    Perform crossover between two genotypes.

    :param parent1: The first genotype.
    :param parent2: The second genotype.
    :param rng: Random number generator.
    :returns: A newly created genotype.
    """
    multineat_rng = _multineat_rng_from_random(rng)
    body = crossover_v1(
            parent1.body,
            parent2.body,
            _MULTINEAT_PARAMS,
            multineat_rng,
            False,
            False,
        )
    brain = brain_crossover(
            parent1.brain,
            parent2.brain,
            0.5,
            first_best
        )
    
    if first_best:
        random_seed = parent1.random_seed
    else:
        random_seed = parent2.random_seed

    return Genotype(
        body,
        brain,
        random_seed
    )

def _multineat_rng_from_random(rng: Random) -> multineat.RNG:
    multineat_rng = multineat.RNG()
    multineat_rng.Seed(rng.randint(0, 2**31))
    return multineat_rng


DbBase = declarative_base()


class DbGenotype(DbBase):
    """Database model for the genotype."""

    __tablename__ = "genotype"

    id = sqlalchemy.Column(
        sqlalchemy.Integer,
        nullable=False,
        unique=True,
        autoincrement=True,
        primary_key=True,
    )

    body_id = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)
    brain_id = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)
    seed_id = sqlalchemy.Column(sqlalchemy.Integer, nullable=False)

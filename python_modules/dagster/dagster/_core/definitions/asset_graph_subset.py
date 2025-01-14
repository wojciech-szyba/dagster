import operator
from collections import defaultdict
from datetime import datetime
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    Iterable,
    Mapping,
    NamedTuple,
    Optional,
    Set,
    Union,
    cast,
)

from dagster import _check as check
from dagster._core.definitions.partition import (
    PartitionsDefinition,
    PartitionsSubset,
)
from dagster._core.definitions.time_window_partitions import PartitionKeysTimeWindowPartitionsSubset
from dagster._core.errors import (
    DagsterDefinitionChangedDeserializationError,
)
from dagster._core.instance import DynamicPartitionsStore
from dagster._serdes.serdes import (
    NamedTupleSerializer,
    SerializableNonScalarKeyMapping,
    whitelist_for_serdes,
)

from .asset_graph import AssetGraph
from .events import AssetKey, AssetKeyPartitionKey


class PartitionsSubsetMappingNamedTupleSerializer(NamedTupleSerializer):
    """Serializes NamedTuples with fields that are mappings containing PartitionsSubsets.

    This is necessary because PartitionKeysTimeWindowPartitionsSubsets are not serializable,
    so we convert them to TimeWindowPartitionsSubsets.
    """

    def before_pack(self, value: NamedTuple) -> NamedTuple:
        replaced_value_by_field_name = {}
        for field_name, field_value in value._asdict().items():
            if isinstance(field_value, Mapping) and all(
                isinstance(v, PartitionsSubset) for v in field_value.values()
            ):
                # PartitionKeysTimeWindowPartitionsSubsets are not serializable, so
                # we convert them to TimeWindowPartitionsSubsets
                subsets_by_key = {
                    k: v.to_time_window_partitions_subset()
                    if isinstance(v, PartitionKeysTimeWindowPartitionsSubset)
                    else v
                    for k, v in field_value.items()
                }

                # If the mapping is keyed by AssetKey wrap it in a SerializableNonScalarKeyMapping
                # so it can be serialized. This can be expanded to other key types in the future.
                if all(isinstance(k, AssetKey) for k in subsets_by_key.keys()):
                    replaced_value_by_field_name[field_name] = SerializableNonScalarKeyMapping(
                        subsets_by_key
                    )

        return value._replace(**replaced_value_by_field_name)


@whitelist_for_serdes(serializer=PartitionsSubsetMappingNamedTupleSerializer)
class AssetGraphSubset(NamedTuple):
    partitions_subsets_by_asset_key: Mapping[AssetKey, PartitionsSubset] = {}
    non_partitioned_asset_keys: AbstractSet[AssetKey] = set()

    @property
    def asset_keys(self) -> AbstractSet[AssetKey]:
        return {
            key for key, subset in self.partitions_subsets_by_asset_key.items() if len(subset) > 0
        } | self.non_partitioned_asset_keys

    @property
    def num_partitions_and_non_partitioned_assets(self):
        return len(self.non_partitioned_asset_keys) + sum(
            len(subset) for subset in self.partitions_subsets_by_asset_key.values()
        )

    def get_partitions_subset(
        self, asset_key: AssetKey, asset_graph: Optional[AssetGraph] = None
    ) -> PartitionsSubset:
        if asset_graph:
            partitions_def = asset_graph.get_partitions_def(asset_key)
            if partitions_def is None:
                check.failed("Can only call get_partitions_subset on a partitioned asset")

            return self.partitions_subsets_by_asset_key.get(
                asset_key, partitions_def.empty_subset()
            )
        else:
            return self.partitions_subsets_by_asset_key[asset_key]

    def iterate_asset_partitions(self) -> Iterable[AssetKeyPartitionKey]:
        for (
            asset_key,
            partitions_subset,
        ) in self.partitions_subsets_by_asset_key.items():
            for partition_key in partitions_subset.get_partition_keys():
                yield AssetKeyPartitionKey(asset_key, partition_key)

        for asset_key in self.non_partitioned_asset_keys:
            yield AssetKeyPartitionKey(asset_key, None)

    def __contains__(self, asset: Union[AssetKey, AssetKeyPartitionKey]) -> bool:
        """If asset is an AssetKeyPartitionKey, check if the given AssetKeyPartitionKey is in the
        subset. If asset is an AssetKey, check if any of partitions of the given AssetKey are in
        the subset.
        """
        if isinstance(asset, AssetKey):
            # check if any keys are in the subset
            partitions_subset = self.partitions_subsets_by_asset_key.get(asset)
            return (partitions_subset is not None and len(partitions_subset) > 0) or (
                asset in self.non_partitioned_asset_keys
            )
        elif asset.partition_key is None:
            return asset.asset_key in self.non_partitioned_asset_keys
        else:
            partitions_subset = self.partitions_subsets_by_asset_key.get(asset.asset_key)
            return partitions_subset is not None and asset.partition_key in partitions_subset

    def to_storage_dict(
        self, dynamic_partitions_store: DynamicPartitionsStore, asset_graph: AssetGraph
    ) -> Mapping[str, object]:
        return {
            "partitions_subsets_by_asset_key": {
                key.to_user_string(): value.serialize()
                for key, value in self.partitions_subsets_by_asset_key.items()
            },
            "serializable_partitions_def_ids_by_asset_key": {
                key.to_user_string(): check.not_none(
                    asset_graph.get_partitions_def(key)
                ).get_serializable_unique_identifier(
                    dynamic_partitions_store=dynamic_partitions_store
                )
                for key, _ in self.partitions_subsets_by_asset_key.items()
            },
            "partitions_def_class_names_by_asset_key": {
                key.to_user_string(): check.not_none(
                    asset_graph.get_partitions_def(key)
                ).__class__.__name__
                for key, _ in self.partitions_subsets_by_asset_key.items()
            },
            "non_partitioned_asset_keys": [
                key.to_user_string() for key in self.non_partitioned_asset_keys
            ],
        }

    def _oper(self, other: "AssetGraphSubset", oper: Callable) -> "AssetGraphSubset":
        """Returns the AssetGraphSubset that results from applying the given operator to the set of
        asset partitions in self and other.

        Note: Not all operators are supported on the underlying PartitionsSubset objects.
        """
        result_partition_subsets_by_asset_key = {**self.partitions_subsets_by_asset_key}
        result_non_partitioned_asset_keys = set(self.non_partitioned_asset_keys)

        for asset_key in other.asset_keys:
            if asset_key in other.non_partitioned_asset_keys:
                check.invariant(asset_key not in self.partitions_subsets_by_asset_key)
                result_non_partitioned_asset_keys = oper(
                    result_non_partitioned_asset_keys, {asset_key}
                )
            else:
                check.invariant(asset_key not in self.non_partitioned_asset_keys)
                subset = (
                    self.get_partitions_subset(asset_key)
                    if asset_key in self.partitions_subsets_by_asset_key
                    else None
                )

                other_subset = other.get_partitions_subset(asset_key)

                if other_subset is not None and subset is not None:
                    result_partition_subsets_by_asset_key[asset_key] = oper(subset, other_subset)

                # Special case operations if either subset is None
                if subset is None and other_subset is not None and oper == operator.or_:
                    result_partition_subsets_by_asset_key[asset_key] = other_subset
                elif subset is not None and other_subset is None and oper == operator.and_:
                    del result_partition_subsets_by_asset_key[asset_key]

        return AssetGraphSubset(
            partitions_subsets_by_asset_key=result_partition_subsets_by_asset_key,
            non_partitioned_asset_keys=result_non_partitioned_asset_keys,
        )

    def __or__(self, other: "AssetGraphSubset") -> "AssetGraphSubset":
        return self._oper(other, operator.or_)

    def __sub__(self, other: "AssetGraphSubset") -> "AssetGraphSubset":
        return self._oper(other, operator.sub)

    def __and__(self, other: "AssetGraphSubset") -> "AssetGraphSubset":
        return self._oper(other, operator.and_)

    def filter_asset_keys(self, asset_keys: AbstractSet[AssetKey]) -> "AssetGraphSubset":
        return AssetGraphSubset(
            partitions_subsets_by_asset_key={
                asset_key: subset
                for asset_key, subset in self.partitions_subsets_by_asset_key.items()
                if asset_key in asset_keys
            },
            non_partitioned_asset_keys=self.non_partitioned_asset_keys & asset_keys,
        )

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, AssetGraphSubset)
            and self.partitions_subsets_by_asset_key == other.partitions_subsets_by_asset_key
            and self.non_partitioned_asset_keys == other.non_partitioned_asset_keys
        )

    def __repr__(self) -> str:
        return (
            "AssetGraphSubset("
            f"non_partitioned_asset_keys={self.non_partitioned_asset_keys}, "
            f"partitions_subsets_by_asset_key={self.partitions_subsets_by_asset_key}"
            ")"
        )

    @classmethod
    def from_asset_partition_set(
        cls,
        asset_partitions_set: AbstractSet[AssetKeyPartitionKey],
        asset_graph: AssetGraph,
    ) -> "AssetGraphSubset":
        partitions_by_asset_key = defaultdict(set)
        non_partitioned_asset_keys = set()
        for asset_key, partition_key in asset_partitions_set:
            if partition_key is not None:
                partitions_by_asset_key[asset_key].add(partition_key)
            else:
                non_partitioned_asset_keys.add(asset_key)

        return AssetGraphSubset(
            partitions_subsets_by_asset_key={
                asset_key: (
                    cast(PartitionsDefinition, asset_graph.get_partitions_def(asset_key))
                    .empty_subset()
                    .with_partition_keys(partition_keys)
                )
                for asset_key, partition_keys in partitions_by_asset_key.items()
            },
            non_partitioned_asset_keys=non_partitioned_asset_keys,
        )

    @classmethod
    def can_deserialize(cls, serialized_dict: Mapping[str, Any], asset_graph: AssetGraph) -> bool:
        serializable_partitions_ids = serialized_dict.get(
            "serializable_partitions_def_ids_by_asset_key", {}
        )

        partitions_def_class_names_by_asset_key = serialized_dict.get(
            "partitions_def_class_names_by_asset_key", {}
        )

        for key, value in serialized_dict["partitions_subsets_by_asset_key"].items():
            asset_key = AssetKey.from_user_string(key)
            partitions_def = asset_graph.get_partitions_def(asset_key)

            if partitions_def is None:
                # Asset had a partitions definition at storage time, but no longer does
                return False

            if not partitions_def.can_deserialize_subset(
                value,
                serialized_partitions_def_unique_id=serializable_partitions_ids.get(key),
                serialized_partitions_def_class_name=partitions_def_class_names_by_asset_key.get(
                    key
                ),
            ):
                return False

        return True

    @classmethod
    def from_storage_dict(
        cls,
        serialized_dict: Mapping[str, Any],
        asset_graph: AssetGraph,
        allow_partial: bool = False,
    ) -> "AssetGraphSubset":
        serializable_partitions_ids = serialized_dict.get(
            "serializable_partitions_def_ids_by_asset_key", {}
        )

        partitions_def_class_names_by_asset_key = serialized_dict.get(
            "partitions_def_class_names_by_asset_key", {}
        )
        partitions_subsets_by_asset_key: Dict[AssetKey, PartitionsSubset] = {}
        for key, value in serialized_dict["partitions_subsets_by_asset_key"].items():
            asset_key = AssetKey.from_user_string(key)

            if asset_key not in asset_graph.all_asset_keys:
                if not allow_partial:
                    raise DagsterDefinitionChangedDeserializationError(
                        f"Asset {key} existed at storage-time, but no longer does"
                    )
                continue

            partitions_def = asset_graph.get_partitions_def(asset_key)

            if partitions_def is None:
                if not allow_partial:
                    raise DagsterDefinitionChangedDeserializationError(
                        f"Asset {key} had a PartitionsDefinition at storage-time, but no longer"
                        " does"
                    )
                continue

            if not partitions_def.can_deserialize_subset(
                value,
                serialized_partitions_def_unique_id=serializable_partitions_ids.get(key),
                serialized_partitions_def_class_name=partitions_def_class_names_by_asset_key.get(
                    key
                ),
            ):
                if not allow_partial:
                    raise DagsterDefinitionChangedDeserializationError(
                        f"Cannot deserialize stored partitions subset for asset {key}. This likely"
                        " indicates that the partitions definition has changed since this was"
                        " stored"
                    )
                continue

            partitions_subsets_by_asset_key[asset_key] = partitions_def.deserialize_subset(value)

        non_partitioned_asset_keys = {
            AssetKey.from_user_string(key) for key in serialized_dict["non_partitioned_asset_keys"]
        } & asset_graph.all_asset_keys

        return AssetGraphSubset(
            partitions_subsets_by_asset_key=partitions_subsets_by_asset_key,
            non_partitioned_asset_keys=non_partitioned_asset_keys,
        )

    @classmethod
    def all(
        cls,
        asset_graph: AssetGraph,
        dynamic_partitions_store: DynamicPartitionsStore,
        current_time: datetime,
    ) -> "AssetGraphSubset":
        return cls.from_asset_keys(
            asset_graph.materializable_asset_keys,
            asset_graph,
            dynamic_partitions_store,
            current_time,
        )

    @classmethod
    def from_asset_keys(
        cls,
        asset_keys: Iterable[AssetKey],
        asset_graph: AssetGraph,
        dynamic_partitions_store: DynamicPartitionsStore,
        current_time: datetime,
    ) -> "AssetGraphSubset":
        partitions_subsets_by_asset_key: Dict[AssetKey, PartitionsSubset] = {}
        non_partitioned_asset_keys: Set[AssetKey] = set()

        for asset_key in asset_keys:
            partitions_def = asset_graph.get_partitions_def(asset_key)
            if partitions_def:
                partitions_subsets_by_asset_key[
                    asset_key
                ] = partitions_def.empty_subset().with_partition_keys(
                    partitions_def.get_partition_keys(
                        dynamic_partitions_store=dynamic_partitions_store,
                        current_time=current_time,
                    )
                )
            else:
                non_partitioned_asset_keys.add(asset_key)

        return AssetGraphSubset(
            partitions_subsets_by_asset_key=partitions_subsets_by_asset_key,
            non_partitioned_asset_keys=non_partitioned_asset_keys,
        )

#
# type specifics
#
import sys
from typing import List, Optional, Type, Union

from typing_extensions import TypedDict

has_pydantic = "pydantic" in sys.modules

# For IntelliSense and Mypy to work, we need to account for possible SQS subclasses
# We need them as subclasses as we must access their message ID or sequence number metadata via dot notation
if has_pydantic:
    from aws_lambda_powertools.utilities.parser.models import DynamoDBStreamRecordModel
    from aws_lambda_powertools.utilities.parser.models import (
        KinesisDataStreamRecord as KinesisDataStreamRecordModel,
    )
    from aws_lambda_powertools.utilities.parser.models import SqsRecordModel

    BatchTypeModels = Optional[
        Union[
            Type[SqsRecordModel],
            Type[DynamoDBStreamRecordModel],
            Type[KinesisDataStreamRecordModel],
        ]
    ]
    BatchSqsTypeModel = Optional[Type[SqsRecordModel]]
else:
    BatchTypeModels = "BatchTypeModels"  # type: ignore
    BatchSqsTypeModel = "BatchSqsTypeModel"  # type: ignore


class PartialItemFailures(TypedDict):
    itemIdentifier: str


class PartialItemFailureResponse(TypedDict):
    batchItemFailures: List[PartialItemFailures]

from typing import FrozenSet, List
import boto3
import boto3.session

from botocore.exceptions import ClientError
from mypy_boto3_iam.type_defs import TagTypeDef

from keydra.providers.base import BaseProvider
from keydra.providers.base import exponential_backoff_retry

from keydra.exceptions import DistributionException
from keydra.exceptions import RotationException

from keydra.logging import get_logger
from mypy_boto3_iam.client import IAMClient


LOGGER = get_logger()

PW_FIELD = 'secret'


def _explain_secret(aws_secret):
    return {
        'provider': 'iam',
        'key': aws_secret['AccessKeyId'],
        f'{PW_FIELD}': aws_secret['SecretAccessKey']
    }


class Client(BaseProvider):
    def __init__(self, session=None, region_name=None, credentials=None):
        if session is None:
            session = boto3.session.Session()

        self._client: IAMClient = session.client('iam')
        self._account_id = None

    def _get_aws_account_id(self) -> str:
        if self._account_id is None:
            self._account_id = boto3.client(
                'sts').get_caller_identity().get('Account')

        return self._account_id

    def _fetch_access_keys(self, user):
        LOGGER.info('Fetching user keys')

        keys_by_id = {
            x['AccessKeyId']: x for x in self._client.list_access_keys(
                UserName=user
            )['AccessKeyMetadata']
        }

        LOGGER.info(
            {
                'message': 'Available keys: {}'.format(
                    ', '.join(
                        [
                            '{} ({})'.format(x['AccessKeyId'], x['Status'])
                            for x in keys_by_id.values()
                        ]
                    )
                ),
                'data': list(keys_by_id.values())
            }
        )

        return keys_by_id

    def _pick_best_candidate(self, keys_by_id):
        LOGGER.info('Picking candidate key for rotation')

        candidate = None
        reason = None

        for kid, key in keys_by_id.items():
            key['last_used'] = self._client.get_access_key_last_used(
                AccessKeyId=kid
            )['AccessKeyLastUsed'].get('LastUsedDate')

            # The inactive key is always the best option for rotation
            if key['Status'] != 'Active':
                candidate = key
                reason = 'inactive'
                break

            if candidate is None:
                candidate = key
                reason = 'initial_option'

            if key['last_used'] is None or candidate['last_used'] is None:
                continue

            # Older keys have preference
            if key['last_used'] < candidate['last_used']:
                candidate = key
                reason = 'last_used'

        LOGGER.info(
            'Key {} elected for rotation. Reason: {}'.format(
                candidate,
                reason
            )
        )

        return candidate

    def _create_access_key(self, user):
        LOGGER.info('Creating new secreds for: {}'.format(user))

        new_creds = self._client.create_access_key(UserName=user)

        LOGGER.info(
            'New secrets for {} successfully created: {}'.format(
                user,
                new_creds['AccessKey']['AccessKeyId']
            )
        )

        return new_creds['AccessKey']

    def _delete_access_key(self, user, key_id):
        LOGGER.info('Deleting key: {}'.format(key_id))

        self._client.delete_access_key(
            AccessKeyId=key_id,
            UserName=user
        )

    def _update_access_key(self, user, key_id, active):
        active_string = 'Active' if active else 'Inactive'

        LOGGER.info('Updating key {}: {}'.format(key_id, active_string))

        self._client.update_access_key(
            AccessKeyId=key_id,
            Status=active_string,
            UserName=user
        )

    def _create_user_if_not_available(self, iam_user, options=None):
        expected_tags: List["TagTypeDef"] = [{
            'Key': 'managedby',
            'Value': 'keydra'
        }]

        if options and options.get('tags'):
            for k, v in options.get('tags').items():
                expected_tags.append({'Key': k, 'Value': v})

        try:
            existing_user = self._client.get_user(UserName=iam_user)

            if existing_user['User'].get('Tags') != expected_tags:
                self._client.tag_user(UserName=iam_user,
                                      Tags=expected_tags)

            return
        except ClientError:
            LOGGER.warn(
                'User "{}" does not exist, attempting to create.'.format(
                    iam_user
                )
            )

        self._client.create_user(
            UserName=iam_user,
            Tags=expected_tags
        )

        LOGGER.info('User "{}" created successfully.'.format(iam_user))

    def _update_user_group_membership(self, iam_user, groups):
        if not isinstance(groups, list):
            groups = [groups]

        rightful_groups = set(groups)
        current_groups = set([])

        try:
            for group in self._client.list_groups_for_user(
                UserName=iam_user
            ).get('Groups', []):
                current_groups.add(group['GroupName'])

        except ClientError as e:  # pragma: no cover
            LOGGER.warn(
                'Not able to list groups for user "{}": {}'.format(iam_user, e)
            )

            return

        group_set = current_groups | rightful_groups

        for group in group_set - rightful_groups:
            try:
                self._client.remove_user_from_group(
                    UserName=iam_user,
                    GroupName=group
                )

                LOGGER.info(
                    'Removed user "{}" from group "{}"'.format(iam_user, group)
                )

            except ClientError as e:  # pragma: no cover
                LOGGER.warn(
                    'Not able to remove user "{}" from group "{}": {}'.format(
                        iam_user, group, e
                    )
                )

        for group in group_set - current_groups:
            try:
                self._client.add_user_to_group(
                    UserName=iam_user,
                    GroupName=group
                )

                LOGGER.info(
                    'Added user "{}" to group "{}"'.format(iam_user, group)
                )

            except ClientError as e:  # pragma: no cover
                LOGGER.warn(
                    'Not able to add user "{}" to group "{}": {}'
                    .format(
                        iam_user, group, e
                    )
                )

    def _make_policy_arn(self, policy_name_with_path: str) -> str:
        return f'arn:aws:iam::{self._get_aws_account_id()}:policy/{policy_name_with_path}'

    def _detach_policy_from_user(self, policy_arn: str, username: str):
        try:
            self._client.detach_user_policy(
                UserName=username,
                PolicyArn=policy_arn)

            LOGGER.info(
                f'Detached policy {policy_arn} from user {username}')
        except ClientError as e:  # pragma: no cover
            LOGGER.warn(
                    f'Failed detaching policy {policy_arn} from user {username}: {e}')

    def _attach_policy_to_user(self, policy_arn: str, username: str):
        try:
            self._client.attach_user_policy(
                UserName=username,
                PolicyArn=policy_arn)

            LOGGER.info(
                f'Attached policy {policy_arn} to user {username}')
        except ClientError as e:  # pragma: no cover
            LOGGER.warn(
                    f'Failed attaching policy {policy_arn} to user {username}: {e}')

    def _update_user_policies(
            self, username, expected_policies: FrozenSet[str]):

        expected_policy_arns: FrozenSet[str] = frozenset({
            self._make_policy_arn(p) for p in expected_policies})

        LOGGER.debug(
            f'Expected policies for IAM user {username}: {expected_policy_arns}')

        try:
            current_policy_arns: FrozenSet[str] = frozenset([
                p.get('PolicyArn', None)
                for p in self._client.list_attached_user_policies(
                    UserName=username)['AttachedPolicies']])
        except ClientError as e:  # pragma: no cover
            LOGGER.warn(
                f'Failed to fetch managed policies attached to IAM user {username}: {e}')
            return

        LOGGER.debug(
            f'Current policies for IAM user {username}: {current_policy_arns}')

        union_policy_arns = expected_policy_arns | current_policy_arns

        for policy_arn in union_policy_arns - current_policy_arns:
            self._attach_policy_to_user(policy_arn, username)

        for policy_arn in union_policy_arns - expected_policy_arns:
            self._detach_policy_from_user(policy_arn, username)

    @exponential_backoff_retry(3)
    def rotate(self, secret):
        try:
            self._create_user_if_not_available(
                secret['key'], secret.get('config'))

            keys_by_id = self._fetch_access_keys(secret['key'])

            # If we have more than 1 keys, let's kill one
            if len(keys_by_id) > 1:
                r_candidate = self._pick_best_candidate(keys_by_id)

                if r_candidate is not None:
                    self._delete_access_key(
                        user=r_candidate['UserName'],
                        key_id=r_candidate['AccessKeyId']
                    )

                    keys_by_id.pop(r_candidate['AccessKeyId'])

            # Let's make the current key inactive, so it is the last one to go
            if len(keys_by_id) == 1:
                r_candidate = list(keys_by_id.values())[0]

                self._update_access_key(
                    user=r_candidate['UserName'],
                    key_id=r_candidate['AccessKeyId'],
                    active=False
                )

            self._update_user_group_membership(
                secret['key'], secret.get('config', {}).get('groups', [])
            )

            self._update_user_policies(secret['key'], frozenset(secret.get(
                'config', {}).get('policies', [])))

            return _explain_secret(self._create_access_key(secret['key']))

        except ClientError as e:  # pragma: no cover
            raise RotationException(e)

    def distribute(self, secret, destination):
        raise DistributionException('IAM does not support distribution')

    @classmethod
    def redact_result(cls, result, spec=None):
        if 'value' in result and PW_FIELD in result['value']:
            result['value'][PW_FIELD] = '***'

        return result

    @classmethod
    def validate_spec(cls, spec):
        valid, msg = BaseProvider.validate_spec(spec)
        if not valid:
            return False, msg
        else:
            if 'config' not in spec:
                return True, 'All good!'

            allowed_options = {'tags': dict, 'groups': list, 'policies': list}

            for oname, otype in allowed_options.items():
                provided_option = spec['config'].get(oname)
                if provided_option and not type(provided_option) == otype:
                    return False, '{} must be a {}'.format(oname, otype)

            unknown_vals = spec['config'].keys() - allowed_options.keys()

            if unknown_vals:
                return (False,
                        'Unsupported values in provider config: {}'
                        .format(unknown_vals))

            return True, 'All good!'

    @classmethod
    def has_creds(cls):
        return False

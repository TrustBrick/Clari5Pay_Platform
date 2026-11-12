#!/usr/bin/env bash
# _archive_aws_profile.sh — AWS execution-identity selection for the archive ops scripts (Demo only).
#
# Enforces the confirmed role-separation decision at RUNTIME. The archive backup job and the
# restore/monitor jobs MUST run under DIFFERENT AWS principals:
#   • backup-write → assumes role  clari5pay-demo-archive-backup-write   (S3 PutObject/Abort + KMS encrypt)
#   • restore-read → assumes role  clari5pay-demo-archive-restore-read   (S3 Get/List    + KMS decrypt)
# Each identity is reached through a named AWS CLI profile that performs STS AssumeRole using EC2
# instance-metadata credentials (credential_source = Ec2InstanceMetadata). NO static access keys.
#
# This file CREATES NO AWS RESOURCES and grants nothing — the two roles, their policies and the KMS
# CMK are provisioned out of band (see ARCHIVE_IAM_EXECUTION_DESIGN.md). It only selects + validates
# a profile the operator has already configured in ~/.aws/config, then exports AWS_PROFILE.
#
# FAIL CLOSED: if the required profile is not configured, this refuses to continue (non-zero, no
# export) rather than silently falling back to the ambient EC2 instance role. A silent fallback would
# collapse write and read onto one identity and defeat the separation.
#
# Profiles are overridable PER ROLE (for local/test/non-prod runs) via two independent variables, so
# a single override can never collapse both roles onto one profile:
#   ARCHIVE_BACKUP_AWS_PROFILE   (default: archive-backup-write)
#   ARCHIVE_RESTORE_AWS_PROFILE  (default: archive-restore-read)
#
# Sourced, never executed:  . "$(dirname "${BASH_SOURCE[0]}")/_archive_aws_profile.sh"

ARCHIVE_BACKUP_AWS_PROFILE_DEFAULT="archive-backup-write"
ARCHIVE_RESTORE_AWS_PROFILE_DEFAULT="archive-restore-read"

# archive_profile_for <role-kind>
#   Prints the resolved profile name for backup-write|restore-read. Returns 3 on an unknown kind.
#   Does NOT touch AWS — safe to call in plan mode purely to document the identity.
archive_profile_for() {
  case "${1:-}" in
    backup-write) printf '%s\n' "${ARCHIVE_BACKUP_AWS_PROFILE:-$ARCHIVE_BACKUP_AWS_PROFILE_DEFAULT}" ;;
    restore-read) printf '%s\n' "${ARCHIVE_RESTORE_AWS_PROFILE:-$ARCHIVE_RESTORE_AWS_PROFILE_DEFAULT}" ;;
    *) return 3 ;;
  esac
}

# archive_select_aws_profile <role-kind>
#   Resolves, validates and EXPORTS AWS_PROFILE for the given role. Return codes:
#     0  selected + exported
#     3  unknown role kind
#     4  named profile not configured (fail closed — AWS_PROFILE left unset)
#     5  aws CLI not found on PATH
#
# On success it also SCRUBS any static credential env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# / AWS_SESSION_TOKEN, plus the legacy AWS_SECURITY_TOKEN). Those outrank a profile in the AWS CLI
# provider chain, so leaving them set could silently bypass the assumed role and run under ambient
# static credentials. Scrubbing them pins the selected named profile as the ONLY credential source.
archive_select_aws_profile() {
  local kind="${1:-}" profile
  if ! profile="$(archive_profile_for "$kind")"; then
    echo "archive_select_aws_profile: unknown role kind '$kind' (expected backup-write|restore-read)" >&2
    return 3
  fi
  if [ -z "$profile" ]; then
    echo "archive_select_aws_profile: empty profile name for '$kind' — refusing ambient credentials" >&2
    return 4
  fi
  if ! command -v aws >/dev/null 2>&1; then
    echo "archive_select_aws_profile: aws CLI not found on PATH (needed to assume the '$kind' role)" >&2
    return 5
  fi
  if ! aws configure list-profiles 2>/dev/null | grep -Fxq -- "$profile"; then
    echo "archive_select_aws_profile: AWS profile '$profile' is not configured for role '$kind'." >&2
    echo "  Configure an STS AssumeRole profile named '$profile' (credential_source=Ec2InstanceMetadata)." >&2
    echo "  Refusing the ambient instance role — that would break backup-write / restore-read separation." >&2
    return 4
  fi
  # Pin the selected profile as the ONLY credential source: drop any static credential env vars that
  # would otherwise outrank AWS_PROFILE in the AWS CLI provider chain and silently bypass the assumed
  # role. Static creds never flow into any subsequent `aws` call from the sourcing script.
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
  export AWS_PROFILE="$profile"
  return 0
}

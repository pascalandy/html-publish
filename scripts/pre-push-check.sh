#!/usr/bin/env bash
set -Eeuo pipefail

fct_is_deletion() {
	local line="${1}"
	local local_ref=""
	local local_oid=""
	local remote_ref=""
	local remote_oid=""
	local extra=""

	IFS=$' \t' read -r local_ref local_oid remote_ref remote_oid extra <<<"${line}" || true
	[[ -n "${local_ref}" && -n "${local_oid}" && -n "${remote_ref}" && -n "${remote_oid}" ]] || return 1
	[[ -z "${extra}" ]] || return 1
	[[ "${local_ref}" == "(delete)" ]] || return 1
	[[ "${local_oid}" =~ ^0+$ ]] || return 1
	[[ ${#local_oid} -eq 40 || ${#local_oid} -eq 64 ]] || return 1
	[[ "${remote_ref}" == refs/* && "${remote_ref}" != "refs/" ]] || return 1
	[[ ${#remote_oid} -eq ${#local_oid} ]] || return 1
	[[ "${remote_oid}" =~ ^[0-9a-f]+$ ]] || return 1
	[[ "${remote_oid}" != "${local_oid}" ]]
}

fct_main() {
	local update_count=0
	local deletion_only=1
	local line=""
	local line_complete=1

	while true; do
		line=""
		if IFS= read -r line; then
			line_complete=1
		elif [[ -n "${line}" ]]; then
			line_complete=0
		else
			break
		fi

		update_count=$((update_count + 1))
		if [[ "${line_complete}" -ne 1 ]] || ! fct_is_deletion "${line}"; then
			deletion_only=0
		fi
	done

	if [[ "${update_count}" -gt 0 && "${deletion_only}" -eq 1 ]]; then
		printf 'pre-push: deletion-only, skipping just check\n'
		return 0
	fi

	exec just check
}

fct_main "$@"

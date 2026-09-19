"""Lucent startup chain package.

Boot-time orchestration that runs before the server pool is created:
the zero-touch tenant-isolation cutover (split migration run, OpenBao
provisioning of the ``lucent_app`` database credential, and connection
as the scoped role). See :mod:`lucent.startup.tenant_chain`.
"""
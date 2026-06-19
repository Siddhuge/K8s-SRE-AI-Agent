# Runbook: ImagePullBackOff / ErrImagePull

## Symptoms
- Pod stuck in `ImagePullBackOff` or `ErrImagePull`
- Events: `Failed to pull image ... not found` or `unauthorized` / `denied`

## Root cause
Either the image tag/digest does not exist in the registry (typo or never-pushed tag),
or the registry rejected authentication (missing/expired imagePullSecret, wrong registry).

## Resolution
1. Verify the exact image:tag exists in the registry.
2. For `unauthorized`/`denied`, repair the imagePullSecret on the pod's ServiceAccount.
3. Re-roll the deployment.

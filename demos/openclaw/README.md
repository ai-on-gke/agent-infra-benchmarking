## Set up auth

```bash
# Set project variables
export PROJECT_ID="akvelon-gke-aieco"
export GSA_NAME="openclaw-ksa"

# Create the Google Service Account
gcloud iam service-accounts create $GSA_NAME \
  --project=$PROJECT_ID \
  --display-name="OpenClaw Vertex AI Service Account"

# Grant the Vertex AI User role to the service account
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Generate and download the JSON key file locally
gcloud iam service-accounts keys create openclaw-sa.json \
  --iam-account="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Upload the JSON key to your GKE cluster as a Secret
kubectl create secret generic openclaw-gcp-sa-key \
  --from-file=credentials.json=openclaw-sa.json \
  --namespace default

# Clean up the local key file for security
rm openclaw-sa.json
```

## Clean up

```bash
export PROJECT_ID="akvelon-gke-aieco"
export GSA_NAME="openclaw-ksa"
export GSA_EMAIL="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects remove-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${GSA_EMAIL}" \
  --role="roles/aiplatform.user"

gcloud iam service-accounts delete $GSA_EMAIL \
  --project=$PROJECT_ID \
  --quiet
```

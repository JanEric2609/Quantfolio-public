function base64UrlToBuffer(value: string): ArrayBuffer {
  const padded = value.padEnd(value.length + ((4 - (value.length % 4)) % 4), "=");
  const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function bufferToBase64Url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function prepareCreationOptions(options: any): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: base64UrlToBuffer(options.challenge),
    user: { ...options.user, id: base64UrlToBuffer(options.user.id) },
    excludeCredentials: options.excludeCredentials?.map((credential: any) => ({
      ...credential,
      id: base64UrlToBuffer(credential.id)
    }))
  };
}

export function prepareRequestOptions(options: any): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: base64UrlToBuffer(options.challenge),
    allowCredentials: options.allowCredentials?.map((credential: any) => ({
      ...credential,
      id: base64UrlToBuffer(credential.id)
    }))
  };
}

export function credentialToJSON(credential: Credential | null): any {
  if (!credential) {
    throw new Error("No passkey credential was returned by the browser.");
  }
  const publicKey = credential as PublicKeyCredential;
  const response = publicKey.response as AuthenticatorAttestationResponse | AuthenticatorAssertionResponse;
  const json: any = {
    id: publicKey.id,
    rawId: bufferToBase64Url(publicKey.rawId),
    type: publicKey.type,
    authenticatorAttachment: publicKey.authenticatorAttachment,
    response: {}
  };
  if ("attestationObject" in response) {
    json.response.attestationObject = bufferToBase64Url(response.attestationObject);
  }
  if ("authenticatorData" in response) {
    json.response.authenticatorData = bufferToBase64Url(response.authenticatorData);
  }
  if ("signature" in response) {
    json.response.signature = bufferToBase64Url(response.signature);
  }
  if ("userHandle" in response && response.userHandle) {
    json.response.userHandle = bufferToBase64Url(response.userHandle);
  }
  json.response.clientDataJSON = bufferToBase64Url(response.clientDataJSON);
  if ("getTransports" in response) {
    json.response.transports = response.getTransports();
  }
  return json;
}

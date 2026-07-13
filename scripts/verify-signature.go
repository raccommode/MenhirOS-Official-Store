package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

// This is the same immutable trust root compiled into Menhir OS.
const publicKeyBase64 = "eal3vson4RMpVQ9Sku01ZfP4pKR7EVKxSWNObDm8Jww="

func main() {
	publicKey, err := base64.StdEncoding.DecodeString(publicKeyBase64)
	if err != nil || len(publicKey) != ed25519.PublicKeySize {
		fmt.Fprintln(os.Stderr, "invalid embedded Menhir store public key")
		os.Exit(1)
	}
	manifest, err := os.ReadFile("store.yaml")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	encodedSignature, err := os.ReadFile("store.yaml.sig")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	signature, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(encodedSignature)))
	if err != nil || len(signature) != ed25519.SignatureSize {
		fmt.Fprintln(os.Stderr, "store.yaml.sig is not a valid base64 Ed25519 signature")
		os.Exit(1)
	}
	if !ed25519.Verify(ed25519.PublicKey(publicKey), manifest, signature) {
		fmt.Fprintln(os.Stderr, "store.yaml signature verification failed")
		os.Exit(1)
	}
	fmt.Println("store.yaml signature verified")
}

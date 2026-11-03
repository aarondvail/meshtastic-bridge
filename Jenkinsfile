// Meshtastic Bridge — Jenkins multibranch pipeline
//
// Loads Vail-Industries-SharedLib so its global vars / src classes are available.
// The stages below are self-contained (plain docker + sh) so the pipeline runs
// as-is; where your shared lib already provides an equivalent Docker build /
// deploy step, swap it in at the marked SHARED-LIB lines.

@Library('Vail-Industries-SharedLib') _
    Vail_Industries_SharedLib {
        org = "Vail-Industries"
        repo_Name = "meshtastic-bridge"
        Jenkins_Node = "BuiltIn"
    }

// pipeline {
//     agent any

//     options {
//         timestamps()
//         disableConcurrentBuilds()
//         buildDiscarder(logRotator(numToKeepStr: '20'))
//         timeout(time: 30, unit: 'MINUTES')
//     }

//     environment {
//         APP_NAME       = 'meshtastic-bridge'
//         PYTHON_VERSION = '3.15.2'
//         IMAGE          = "${APP_NAME}"
//         DEPLOY_DIR     = '/opt/port/Meshtastic-Bridge'
//         CONTAINER_NAME = "${APP_NAME}"
//         // Set to e.g. /dev/ttyUSB0 for a USB-attached radio; leave empty for TCP radios
//         SERIAL_DEVICE  = ''
//     }

//     stages {
//         stage('Checkout') {
//             steps {
//                 checkout scm
//                 script {
//                     env.GIT_SHORT = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
//                     env.IMAGE_TAG = "${env.BUILD_NUMBER}-${env.GIT_SHORT}"
//                     currentBuild.displayName = "#${env.BUILD_NUMBER} ${env.GIT_SHORT}"
//                 }
//             }
//         }

//         stage('Build image') {
//             steps {
//                 // SHARED-LIB: replace with your lib's Docker build step if it has one
//                 sh '''
//                     docker build \
//                         --pull \
//                         --build-arg PYTHON_VERSION="$PYTHON_VERSION" \
//                         --label "org.opencontainers.image.revision=$GIT_COMMIT" \
//                         --label "org.opencontainers.image.source=$GIT_URL" \
//                         -t "$IMAGE:$IMAGE_TAG" \
//                         .
//                 '''
//             }
//         }

//         stage('Test') {
//             steps {
//                 // Runs the stdlib unittest suite inside the image that will be deployed
//                 sh '''
//                     docker run --rm \
//                         -v "$WORKSPACE/tests:/code/tests:ro" \
//                         "$IMAGE:$IMAGE_TAG" \
//                         sh -c 'python --version && python -m unittest discover -s tests -v'
//                 '''
//             }
//         }

//         stage('Deploy') {
//             when { branch 'master' }
//             steps {
//                 // SHARED-LIB: replace with your lib's Docker deploy step if it has one
//                 sh '''
//                     set -eu

//                     if [ ! -f "$DEPLOY_DIR/config.yaml" ]; then
//                         echo "ERROR: $DEPLOY_DIR/config.yaml not found on the host" >&2
//                         exit 1
//                     fi

//                     set -- --name "$CONTAINER_NAME" \
//                            --restart unless-stopped \
//                            --network host \
//                            -v "$DEPLOY_DIR/config.yaml:/code/config.yaml:ro"

//                     # Optional extras on the host
//                     [ -d "$DEPLOY_DIR/keys" ]       && set -- "$@" -v "$DEPLOY_DIR/keys:/code/keys:ro"
//                     [ -f "$DEPLOY_DIR/bridge.env" ] && set -- "$@" --env-file "$DEPLOY_DIR/bridge.env"
//                     [ -n "$SERIAL_DEVICE" ]         && set -- "$@" --device "$SERIAL_DEVICE"

//                     docker tag "$IMAGE:$IMAGE_TAG" "$IMAGE:latest"
//                     docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
//                     docker run -d "$@" "$IMAGE:$IMAGE_TAG"

//                     # Fail the build if the bridge dies on startup (bad config, etc.)
//                     sleep 10
//                     if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]; then
//                         docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
//                         echo "ERROR: $CONTAINER_NAME is not running after deploy" >&2
//                         exit 1
//                     fi
//                     docker logs --tail 20 "$CONTAINER_NAME"
//                 '''
//             }
//         }
//     }

//     post {
//         always {
//             // Keep the 3 newest image tags, remove the rest
//             sh '''
//                 docker images "$IMAGE" --format '{{.Tag}}' \
//                     | grep -E '^[0-9]+-' | sort -t- -k1,1nr | tail -n +4 \
//                     | xargs -r -I{} docker rmi "$IMAGE:{}" || true
//                 docker image prune -f --filter "label=org.opencontainers.image.source=$GIT_URL" || true
//             '''
//         }
//     }
// }
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// Encapsulate all avatar logic into a class to make it a self-contained module.
export default class LunaAvatar {
    constructor() {
        // --- CLASS PROPERTIES ---
        this.scene = null;
        this.camera = null;
        this.renderer = null;
        this.avatar = null;
        this.analyser = null;
        this.dataArray = null;
        this.mixer = null;
        this.clock = new THREE.Clock();
        this.mouthMorphTargetIndex = -1;

        this.chestBone = null;
        this.leftEyeBone = null;
        this.rightEyeBone = null;
        this.neckBone = null;
        this.headBone = null;

        this.mouseX = 0;
        this.mouseY = 0;

        this.currentMouthOpenness = 0;
        this.isSpeaking = false;
        this.silenceTimer = 0;

        // --- CONFIGURATION CONSTANTS ---
        this.NOISE_THRESHOLD = 1;
        this.MOUTH_GAIN = .9;
        this.SILENCE_DELAY = 1.0; // Seconds of silence before reverting to idle
        this.HEAD_MAX_SCALE_INCREASE = 0;
        this.BODY_MAX_SCALE_INCREASE = 0;

        // --- INITIALIZATION ---
        this._init();
        this._animate(); // Start the animation loop
    }

    _init() {
        this.scene = new THREE.Scene();
        this.scene.background = null;

        this.camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 100);
        this.camera.position.set(0, 1.6, 3.5);
        this.camera.lookAt(0, 1, 0);

        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.renderer.domElement.id = 'theia-form';
        const renderSize = 800;
        this.renderer.setSize(renderSize, renderSize);
        this.camera.aspect = 1;
        this.renderer.domElement.style.position = 'fixed';
        this.renderer.domElement.style.top = 0;
        this.renderer.domElement.style.left = 0;
        this.renderer.domElement.style.right = 0;
        this.renderer.domElement.style.bottom = 0;
        this.renderer.domElement.style.width = '100%';
        this.renderer.domElement.style.height = '100%';
        

        // 2. Imposta la dimensione iniziale del renderer in base alla finestra.
        this.renderer.setSize(window.innerWidth, window.innerHeight);

        // 3. Calcola l'aspect ratio corretto per la camera.
        this.camera.aspect = window.innerWidth / window.innerHeight;
        this.camera.updateProjectionMatrix();
        this.camera.updateProjectionMatrix();
        document.body.appendChild(this.renderer.domElement);

        const hemisphereLight = new THREE.HemisphereLight(0xffffff, 0x444444, 2);
        hemisphereLight.position.set(0, 3, 2);
        this.scene.add(hemisphereLight);

        const directionalLight = new THREE.DirectionalLight(0xffffff, 3);
        directionalLight.position.set(-1, 2, 4);
        this.scene.add(directionalLight);

        const loader = new GLTFLoader();
        const modelUrl = 'https://models.readyplayer.me/68caa3f561035c30826eed63.glb';
        loader.load(modelUrl, (gltf) => {
            this.avatar = gltf.scene;
            this.scene.add(this.avatar);
            this.avatar.position.y = 0;

            this.mixer = new THREE.AnimationMixer(this.avatar);
            if (gltf.animations && gltf.animations.length) {
                const idleAnimation = gltf.animations.find(anim => anim.name.toLowerCase().includes('idle')) || gltf.animations[0];
                const action = this.mixer.clipAction(idleAnimation);
                action.play();
            }

            this.avatar.traverse(child => {
                if (child.isMesh && child.morphTargetDictionary) {
                    if ('jawOpen' in child.morphTargetDictionary) this.mouthMorphTargetIndex = child.morphTargetDictionary['jawOpen'];
                    else if ('mouthOpen' in child.morphTargetDictionary) this.mouthMorphTargetIndex = child.morphTargetDictionary['mouthOpen'];
                    else if ('viseme_aa' in child.morphTargetDictionary) this.mouthMorphTargetIndex = child.morphTargetDictionary['viseme_aa'];
                }
                if (child.isBone) {
                    switch (child.name) {
                        case 'Spine2': this.chestBone = child; break;
                        case 'LeftEye': this.leftEyeBone = child; break;
                        case 'RightEye': this.rightEyeBone = child; break;
                        case 'Neck': this.neckBone = child; break;
                        case 'Head': this.headBone = child; break;
                    }
                }
            });

            const leftShoulder = this.avatar.getObjectByName('LeftShoulder');
            const rightShoulder = this.avatar.getObjectByName('RightShoulder');
            if (leftShoulder && rightShoulder) {
                const armRotationAmount = 0.5;
                leftShoulder.rotation.y = -armRotationAmount;
                rightShoulder.rotation.y = armRotationAmount;
            }

            if (this.mouthMorphTargetIndex === -1) console.warn("Mouth morph target not found.");
        }, undefined, (err) => console.error("Error loading model:", err));

        window.addEventListener('resize', () => this._onWindowResize());
        window.addEventListener('mousemove', (event) => {
            this.mouseX = (event.clientX / window.innerWidth) * 2 - 1;
            this.mouseY = -(event.clientY / window.innerHeight) * 2 + 1;
        });
    }

    // Public method to connect the audio stream
    async connectStream(stream) {
        try {
            const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const source = audioCtx.createMediaStreamSource(stream);
            this.analyser = audioCtx.createAnalyser();
            this.analyser.fftSize = 128;
            source.connect(this.analyser);
            this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
            console.log("🎤 Avatar audio stream connected!");
        } catch (err) {
            console.error("Avatar microphone error: " + err.message);
        }
    }
    
    _animate() {
        requestAnimationFrame(() => this._animate());
        const delta = this.clock.getDelta();
        const elapsedTime = this.clock.getElapsedTime();
    
        if (this.mixer) this.mixer.update(delta);
    
        if (this.leftEyeBone && this.rightEyeBone) {
            const blinkTime = elapsedTime % 4;
            const isBlinking = blinkTime > 0 && blinkTime < 0.1;
            const scale = isBlinking ? 0.1 : 1;
            this.leftEyeBone.scale.y = scale;
            this.rightEyeBone.scale.y = scale;
        }
    
        if (this.avatar) {
            let targetMouthOpenness = 0;
            let avgVolume = 0;
    
            if (this.analyser) {
                this.analyser.getByteTimeDomainData(this.dataArray);
                let sumSquares = 0;
                for (let i = 0; i < this.dataArray.length; i++) {
                    const val = (this.dataArray[i] - 128) / 128;
                    sumSquares += val * val;
                }
                avgVolume = Math.sqrt(sumSquares / this.dataArray.length) * 100;
    
                if (avgVolume > this.NOISE_THRESHOLD) {
                    this.isSpeaking = true;
                    this.silenceTimer = 0;
                } else {
                    this.silenceTimer += delta;
                    if (this.silenceTimer > this.SILENCE_DELAY) {
                        this.isSpeaking = false;
                    }
                }
            }
            
            if (this.isSpeaking) {
                const dynamicIntensity = Math.min(avgVolume / 50, 1.0);
                const slowSwaySpeed = 0.5;
    
                const swayAmount = dynamicIntensity * 0.05;
                this.camera.position.x = Math.sin(elapsedTime * slowSwaySpeed) * swayAmount;
                this.camera.position.z = 3.5 + Math.cos(elapsedTime * slowSwaySpeed) * swayAmount;
    
                const rotationAmount = dynamicIntensity * 0.02;
                this.camera.rotation.z = Math.sin(elapsedTime * slowSwaySpeed * 0.7) * rotationAmount;
                
                if (this.headBone) {
                    const targetScale = 1 + (dynamicIntensity * this.HEAD_MAX_SCALE_INCREASE); 
                    this.headBone.scale.lerp(new THREE.Vector3(targetScale, targetScale, targetScale), 0.2);
                }
    
                const targetBodyScale = 1 + (dynamicIntensity * this.BODY_MAX_SCALE_INCREASE);
                this.avatar.scale.lerp(new THREE.Vector3(targetBodyScale, targetBodyScale, targetBodyScale), 0.2);
                
                targetMouthOpenness = (avgVolume / 4) * this.MOUTH_GAIN;
    
            } else {
                this.camera.position.lerp(new THREE.Vector3(0, 1.6, 3.5), 0.05);
                this.camera.rotation.z *= 0.95; 
    
                if (this.headBone) this.headBone.scale.lerp(new THREE.Vector3(1, 1, 1), 0.2);
                this.avatar.scale.lerp(new THREE.Vector3(1, 1, 1), 0.2);
    
                const swayFrequency = 0.4;
                const swayIntensity = 0.04;
                this.avatar.rotation.y = Math.sin(elapsedTime * swayFrequency) * swayIntensity;
                if (this.chestBone) {
                    const breathSpeed = 0.5;
                    const breathIntensity = 0.06;
                    this.chestBone.rotation.x = Math.sin(elapsedTime * breathSpeed) * breathIntensity;
                }
            }
            
            this.camera.lookAt(0, 1, 0);
    
            const smoothingFactor = 0.2;
            this.currentMouthOpenness += (targetMouthOpenness - this.currentMouthOpenness) * smoothingFactor;
    
            this.avatar.traverse(child => {
                if (child.isMesh && child.morphTargetInfluences && this.mouthMorphTargetIndex !== -1) {
                    child.morphTargetInfluences[this.mouthMorphTargetIndex] = this.currentMouthOpenness;
                }
            });
    
            if (this.neckBone && this.headBone) {
                const targetHeadRotationY = this.isSpeaking ? 0 : this.mouseX * 0.5;
                const targetHeadRotationX = this.isSpeaking ? 0 : -this.mouseY * 0.3;
    
                const headSmoothingFactor = 0.08;
                this.neckBone.rotation.y += (targetHeadRotationY - this.neckBone.rotation.y) * headSmoothingFactor;
                this.neckBone.rotation.x += (targetHeadRotationX - this.neckBone.rotation.x) * headSmoothingFactor;
                this.headBone.rotation.y += (targetHeadRotationY - this.headBone.rotation.y) * headSmoothingFactor;
                this.headBone.rotation.x += (targetHeadRotationX - this.headBone.rotation.x) * headSmoothingFactor;
            }
        }
    
        this.renderer.render(this.scene, this.camera);
    }

    _onWindowResize() {
        const canvas = this.renderer.domElement;
        this.camera.aspect = canvas.clientWidth / canvas.clientHeight;
        this.camera.updateProjectionMatrix();
    }
}
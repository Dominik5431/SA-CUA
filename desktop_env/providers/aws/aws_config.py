IMAGE_ID_MAP = {
    "us-east-2": {"reddit":"ami-0e542ab10c542c23e","agentcompany":"ami-06a0481b8e8344be2"}, 'us-east-1': {"osworld":"ami-0628fc3d1e3ae597d"}
}

INSTANCE_TYPE = {"reddit": "t3a.2xlarge","agentcompany": "t3a.2xlarge","osworld":"t3.medium"}



NETWORK_INTERFACE_MAP = {
    "us-east-2": [
        {
            "SubnetId": "subnet-0a39d0fcb82bc8176",
            "AssociatePublicIpAddress": True,
            "DeviceIndex": 0,
            "Groups": [
                "sg-02a3d911531109bec"
            ]
        }
    ]
}


BLOCK_DEVICE_MAPPINGS = {
    "reddit":
        [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 200,  
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True
                }
            }
        ],
    "agentcompany":
        [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 100,  
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True
                }
            }
        ],
    "osworld":
        [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 50,  
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True
                }
            }
        ]

}
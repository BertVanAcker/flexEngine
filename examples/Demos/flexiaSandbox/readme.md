# flexIA Sandbox - flexCloud-flexEngine interactions
  
  This directory contains a small demonstration of the flexEngine for servers, SBC and soft-PLC core.
Within this demo, we target 3 devices. Each of the devices runs the **flexAgent**.
  
![flexCloud_flexEngine_demo.png](assets%2FflexCloud_flexEngine_demo.png)

  It demonstrates the following elements

  **- flexCloud:**
      
     - Push the application to multiple devices
     - Execute the application on multiple devices

 **- flexEngine:**
      
     - Registration of the application
     - Device vitals monitoring & centralized logging
     - Standalone application handling

  ## flexCloud metadata
    
  - **Project**: oberon5@sandbox
  - **Key**: 3fg889
  - **Db**: 7

## Clean the demonstrator workspace

  - Before starting the demo, please make sure to clean the workspace from previous builds:
  
  ```commandline
    python clean.py
```

## Spin up the flexAgents
 
  In order to run this demo, you need to spin up the flexAgents of the mock devices.
  

  ## Application deployment
  
  - This application mocks the commands comming from **flexCloud**:
    - Push the application repository to the target devices
    - Request the flexEngine to register the application
  
  ```commandline
    python push.py
```
  
  ## Run apllication from flexCloud

  - This application mocks the commands comming from **flexCloud**:
    - Request to build the application on the target devices
    - Request to start the application on the target devices
  
  ```commandline
    python run.py
```
 
  ## Stop apllication from flexCloud

  - This application mocks the commands comming from **flexCloud**:
    - Request to stop the application on the target devices
  
  ```commandline
    python stop.py
```
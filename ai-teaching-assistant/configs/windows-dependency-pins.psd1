@{
    Pins = @(
        @{
            Requirement = "regex==2026.9.29"
            Services = @("*")
        },
        @{
            Requirement = "grpcio==1.85.1"
            Services = @("*")
        },
        @{
            Requirement = "xxhash==4.0.2"
            Services = @("rag-service")
        },
        @{
            Requirement = "ml-dtypes==0.6.0"
            Services = @("audio-analyzer")
        }
    )
}